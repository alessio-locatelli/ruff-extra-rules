from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import argparse
    from collections.abc import Iterable


class ConfigError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _OptionName:
    name: str

    @property
    def keyword(self) -> str:
        return self.name.replace("-", "_")

    def flag(self, check_id: str) -> str:
        return f"--{check_id}-{self.name}"

    def dest(self, check_id: str) -> str:
        return f"{check_id}-{self.name}".replace("-", "_")


@dataclass(frozen=True, slots=True)
class EnumOption[E: Enum](_OptionName):
    values: type[E]
    default: E
    help: str

    @property
    def choices(self) -> tuple[str, ...]:
        return tuple(member.name.lower() for member in self.values)

    def argument_kwargs(self) -> dict[str, Any]:
        return {"choices": self.choices}

    def coerce(self, raw: object, source: str) -> E:
        if isinstance(raw, str) and raw.lower() in self.choices:
            return self.values[raw.upper()]
        expected = ", ".join(f"`{choice}`" for choice in self.choices)
        message = f"Invalid value {raw!r} for `{self.name}` from {source}; expected one of: {expected}"
        raise ConfigError(message)


@dataclass(frozen=True, slots=True)
class IntOption(_OptionName):
    default: int
    minimum: int
    help: str

    def argument_kwargs(self) -> dict[str, Any]:
        return {"type": int, "metavar": "N"}

    def coerce(self, raw: object, source: str) -> int:
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= self.minimum:
            return raw
        message = (
            f"Invalid value {raw!r} for `{self.name}` from {source}; expected an integer of at least {self.minimum}"
        )
        raise ConfigError(message)


type CheckOption = EnumOption[Enum] | IntOption


def add_check_arguments(parser: argparse.ArgumentParser, check_id: str, options: Iterable[CheckOption]) -> None:
    for option in options:
        parser.add_argument(
            option.flag(check_id),
            dest=option.dest(check_id),
            default=None,
            help=option.help,
            **option.argument_kwargs(),
        )
