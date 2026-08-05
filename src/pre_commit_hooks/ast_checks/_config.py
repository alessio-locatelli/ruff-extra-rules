"""`pyproject.toml` discovery, validation, and precedence over the command line.

See `docs/adr/0045-pyproject-toml-configuration.md`.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._discovery import ExcludePattern
from ._options import ConfigError

if TYPE_CHECKING:
    import argparse
    from collections.abc import Iterable, Mapping, Sequence
    from enum import Enum

    from ._base import ASTCheck
    from ._options import CheckOption

TABLE_NAME = "ruff-extra-rules"
CONFIG_FILENAME = "pyproject.toml"
CLI_SOURCE = "the CLI"

_GLOBAL_KEYS = frozenset({"exclude", "fix", "ignore", "select"})


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    """See `docs/adr/0045-pyproject-toml-configuration.md`."""

    root: Path
    select: set[str] | None
    ignore: set[str] | None
    exclude: list[ExcludePattern]
    fix: bool
    check_kwargs: dict[str, dict[str, Any]]


def _git_boundary(start: Path) -> Path | None:
    """`.git` is a directory in an ordinary clone and a file in a worktree
    or submodule, so existence rather than type is the test.
    """
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return directory
    return None


def discover(start: Path) -> tuple[Path, Mapping[str, Any]] | None:
    """See `docs/adr/0045-pyproject-toml-configuration.md`."""
    boundary = _git_boundary(start)
    for directory in (start, *start.parents):
        candidate = directory / CONFIG_FILENAME
        if candidate.is_file():
            table = read_table(candidate)
            if table is not None:
                return candidate, table
        if directory == boundary:
            break
    return None


def read_table(path: Path) -> Mapping[str, Any] | None:
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except OSError as error:
        message = f"Could not read `{path}`: {error}"
        raise ConfigError(message) from error
    except tomllib.TOMLDecodeError as error:
        message = f"Failed to parse `{path}`: {error}"
        raise ConfigError(message) from error

    tool = document.get("tool")
    table = tool.get(TABLE_NAME) if isinstance(tool, dict) else None
    if table is None:
        return None
    if not isinstance(table, dict):
        message = f"`[tool.{TABLE_NAME}]` in `{path}` must be a table, not {type(table).__name__}"
        raise ConfigError(message)
    return table


def _quoted(values: Iterable[str]) -> str:
    return ", ".join(f"`{value}`" for value in sorted(values))


def _table_bool(table: Mapping[str, Any], key: str, source: str, *, default: bool) -> bool:
    if key not in table:
        return default
    value = table[key]
    if not isinstance(value, bool):
        message = f"Invalid value for `{key}` from {source}; expected a boolean, got {type(value).__name__}"
        raise ConfigError(message)
    return value


def _table_string_list(table: Mapping[str, Any], key: str, source: str) -> list[str] | None:
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        message = f"Invalid value for `{key}` from {source}; expected a list of strings"
        raise ConfigError(message)
    return value


def _split_check_ids(values: list[str]) -> set[str]:
    """See `docs/adr/0019-behavioral-contract-audit-configuration-and-discovery.md`."""
    return {check_id.strip() for value in values for check_id in value.split(",") if check_id.strip()}


def _validate_check_ids(check_ids: Iterable[str], key: str, source: str, known: Iterable[str]) -> None:
    unknown = set(check_ids) - set(known)
    if unknown:
        message = f"Unknown check {_quoted(unknown)} in `{key}` from {source}; expected one of: {_quoted(known)}"
        raise ConfigError(message)


def _table_check_ids(table: Mapping[str, Any], key: str, source: str, known: Iterable[str]) -> set[str] | None:
    configured = _table_string_list(table, key, source)
    if configured is None:
        return None
    _validate_check_ids(configured, key, source, known)
    return set(configured)


def _cli_check_ids(cli_values: list[str], key: str, known: Iterable[str]) -> set[str]:
    selected = _split_check_ids(cli_values)
    if not selected:
        message = f"Invalid value for `--{key}` from {CLI_SOURCE}; expected at least one check name"
        raise ConfigError(message)
    _validate_check_ids(selected, f"--{key}", CLI_SOURCE, known)
    return selected


def _load(args: argparse.Namespace, cwd: Path) -> tuple[Mapping[str, Any], Path, str]:
    if args.isolated:
        if args.config is not None:
            message = "`--config` and `--isolated` cannot be used together"
            raise ConfigError(message)
        return {}, cwd, CLI_SOURCE

    if args.config is not None:
        # `path.parent` anchors every config-file `exclude` pattern, and a
        # relative anchor can never match the absolute paths those patterns
        # are compared against. See ADR-0046.
        path = Path(os.path.abspath(args.config))  # noqa: PTH100
        if not path.is_file():
            message = f"Could not read `{path}`: no such file"
            raise ConfigError(message)
        table = read_table(path)
        if table is None:
            message = f"`{path}` has no `[tool.{TABLE_NAME}]` table"
            raise ConfigError(message)
        return table, path.parent, f"`{path}`"

    found = discover(cwd)
    if found is None:
        return {}, cwd, CLI_SOURCE
    path, table = found
    return table, path.parent, f"`{path}`"


def resolve(
    args: argparse.Namespace,
    *,
    enabled_check_classes: Sequence[type[ASTCheck]],
    all_check_classes: Sequence[type[ASTCheck]],
) -> ResolvedConfig:
    """See `docs/adr/0045-pyproject-toml-configuration.md`."""
    cwd = Path.cwd()
    table, root, source = _load(args, cwd)

    known_check_ids = {check_class().check_id: check_class for check_class in all_check_classes}
    unknown_keys = set(table) - _GLOBAL_KEYS - set(known_check_ids)
    if unknown_keys:
        valid = _GLOBAL_KEYS | set(known_check_ids)
        message = (
            f"Unknown field {_quoted(unknown_keys)} in `[tool.{TABLE_NAME}]` from {source}; "
            f"expected one of: {_quoted(valid)}"
        )
        raise ConfigError(message)

    # Validated in full before precedence is applied; see ADR-0045.
    configured_fix = _table_bool(table, "fix", source, default=False)
    configured_exclude = _table_string_list(table, "exclude", source)
    configured_select = _table_check_ids(table, "select", source, known_check_ids)
    configured_ignore = _table_check_ids(table, "ignore", source, known_check_ids)

    if args.exclude is not None:
        exclude = [ExcludePattern(pattern.strip(), cwd) for pattern in args.exclude.split(",") if pattern.strip()]
    else:
        exclude = [ExcludePattern(pattern, root) for pattern in configured_exclude or ()]

    enabled_check_ids = {check_class().check_id for check_class in enabled_check_classes}
    check_kwargs: dict[str, dict[str, Any]] = {}
    for check_id, check_class in known_check_ids.items():
        sub_table = _check_table(table, check_id, source)
        option_names = {option.name for option in check_class.OPTIONS}
        unknown_options = set(sub_table) - option_names
        if unknown_options:
            message = (
                f"Unknown field {_quoted(unknown_options)} in `[tool.{TABLE_NAME}.{check_id}]` from {source}; "
                f"expected one of: {_quoted(option_names)}"
            )
            raise ConfigError(message)
        configured_options = {
            option.name: option.coerce(sub_table[option.name], source)
            for option in check_class.OPTIONS
            if option.name in sub_table
        }
        if check_id not in enabled_check_ids:
            continue
        kwargs = {
            option.name: _resolve_option_value(args, check_id, option, configured_options)
            for option in check_class.OPTIONS
        }
        if kwargs:
            check_kwargs[check_id] = kwargs

    return ResolvedConfig(
        root=root,
        select=_cli_check_ids(args.select, "select", known_check_ids) if args.select is not None else configured_select,
        ignore=_cli_check_ids(args.ignore, "ignore", known_check_ids) if args.ignore is not None else configured_ignore,
        exclude=exclude,
        fix=configured_fix if args.fix is None else args.fix,
        check_kwargs=check_kwargs,
    )


def _check_table(table: Mapping[str, Any], check_id: str, source: str) -> Mapping[str, Any]:
    configured = table.get(check_id, {})
    if not isinstance(configured, dict):
        message = f"`[tool.{TABLE_NAME}.{check_id}]` in {source} must be a table, not {type(configured).__name__}"
        raise ConfigError(message)
    return configured


def _resolve_option_value(
    args: argparse.Namespace,
    check_id: str,
    option: CheckOption,
    configured: Mapping[str, Enum],
) -> Enum:
    cli_value = getattr(args, option.dest(check_id), None)
    if cli_value is not None:
        return option.coerce(cli_value, CLI_SOURCE)
    return configured.get(option.name, option.default)
