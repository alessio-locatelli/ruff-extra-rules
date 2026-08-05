"""Option declarations shared by the CLI parser and the `pyproject.toml` loader.

A check declares each option once, here; the command-line flag, the TOML
key, the accepted values, and the check's own `__init__` kwarg are all
derived from that declaration. See
`docs/adr/0047-declarative-option-descriptors.md`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse
    from collections.abc import Iterable


class ConfigError(Exception):
    """Reported with exit code 2; see `docs/adr/0045-pyproject-toml-configuration.md`."""


@dataclass(frozen=True, slots=True)
class EnumOption[E: Enum]:
    name: str
    values: type[E]
    default: E
    help: str

    def flag(self, check_id: str) -> str:
        return f"--{check_id}-{self.name}"

    def dest(self, check_id: str) -> str:
        return f"{check_id}-{self.name}".replace("-", "_")

    @property
    def choices(self) -> tuple[str, ...]:
        return tuple(member.name.lower() for member in self.values)

    def coerce(self, raw: object, source: str) -> E:
        if isinstance(raw, str) and raw.lower() in self.choices:
            return self.values[raw.upper()]
        expected = ", ".join(f"`{choice}`" for choice in self.choices)
        message = f"Invalid value {raw!r} for `{self.name}` from {source}; expected one of: {expected}"
        raise ConfigError(message)


type CheckOption = EnumOption[Enum]


def add_check_arguments(parser: argparse.ArgumentParser, check_id: str, options: Iterable[CheckOption]) -> None:
    """`default=None` keeps an unset flag distinguishable from one given its
    default value; see `docs/adr/0047-declarative-option-descriptors.md`.
    """
    for option in options:
        parser.add_argument(
            option.flag(check_id),
            dest=option.dest(check_id),
            choices=option.choices,
            default=None,
            help=option.help,
        )
