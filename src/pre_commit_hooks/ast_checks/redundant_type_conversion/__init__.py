"""Check for redundant builtin type-conversion calls (TRI006).

TRI006: flags a builtin type/collection constructor call — `str(...)`,
`list(...)`, and eight others — that's a no-op given the argument's real,
statically-known type, including across file/import boundaries. Detection
delegates to Astral's `ty` type checker (see `session.py`) rather than
approximating type information locally.

Requires `ty` on `PATH` (e.g. `uv tool install ty`, or as your own
project's dev dependency) — see `docs/rules/redundant-type-conversion.md`.

Inline ignore: `# pytriage: ignore=TRI006`. A line already carrying a
third-party type-suppression comment (`# type: ignore`, `# pyright:
ignore`, `# ty: ignore`) is always skipped too — see this package's own
`docs/rules/redundant-type-conversion.md` for why.

Detect-only: this check ships no autofix in this version.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from pre_commit_hooks.ast_checks._base import BaseCheck, Violation, find_ignored_lines, ignore_pattern_for

from .analysis import RedundantConversion, decide_candidates
from .confidence import ConfidenceLevel
from .session import get_session

if TYPE_CHECKING:
    import argparse
    import ast
    from pathlib import Path

# Format: # pytriage: ignore=TRI006
IGNORE_PATTERN = ignore_pattern_for("TRI006")

# A third-party type-suppression comment on a candidate's own line: see
# decide_candidates()'s own docstring / this check's rule doc for why a
# suppressed line is always skipped outright rather than trusting `ty`'s
# own diagnostics to already reflect the suppression.
THIRD_PARTY_IGNORE_PATTERN = re.compile(r"#\s*(?:type|pyright|ty)\s*:\s*ignore\b", re.IGNORECASE)

ERROR_CODE = "TRI006"
CHECK_ID = "redundant-type-conversion"

_CONSTRUCTOR_NAMES = ("str", "int", "float", "bool", "bytes", "frozenset", "tuple", "list", "dict", "set", "bytearray")


def _format_message(item: RedundantConversion) -> str:
    constructor = item.candidate.constructor
    return (
        f"Redundant `{constructor}(...)` conversion: the argument is already `{item.argument_type}`, so "
        f"wrapping it in `{constructor}()` has no effect. Or add '# pytriage: ignore={ERROR_CODE}' to suppress."
    )


class RedundantTypeConversionCheck(BaseCheck):
    __slots__ = ("_level",)

    def __init__(self, level: ConfidenceLevel = ConfidenceLevel.CONSERVATIVE) -> None:
        self._level = level

    @property
    def check_id(self) -> str:
        return CHECK_ID

    @property
    def error_code(self) -> str:
        return ERROR_CODE

    @property
    def cacheable(self) -> bool:
        """This check's result for one file can depend on another file's
        current content (e.g. a parameter type `ty` resolves through a
        cross-file import) — see ADR-0034 for why that means it must
        always re-analyze every file it's given, never reading or writing
        the shared per-file cache.
        """
        return False

    def get_prefilter_pattern(self) -> list[str] | None:
        return [f"{name}(" for name in _CONSTRUCTOR_NAMES]

    @classmethod
    def add_cli_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--redundant-type-conversion-level",
            choices=["conservative", "permissive"],
            default="conservative",
            help=(
                "How eagerly redundant-type-conversion (TRI006) reports a violation. 'conservative' (default) "
                "flags only the seven constructors whose result can't alias or share mutable state with their "
                "argument (str/int/float/bool/bytes/frozenset/tuple), and only an exact type match. "
                "'permissive' also flags the four copy-producing constructors (list/dict/set/bytearray) and a "
                "broader, structural/protocol-satisfying match -- an explicit opt-in to the aliasing/mutation "
                "risk a copy-producing constructor's result can carry."
            ),
        )

    @classmethod
    def cli_kwargs_from_args(cls, args: argparse.Namespace) -> dict[str, Any]:
        return {"level": ConfidenceLevel[args.redundant_type_conversion_level.upper()]}

    def check(self, filepath: Path, tree: ast.Module, source: str) -> list[Violation]:
        ignored_lines = find_ignored_lines(source, IGNORE_PATTERN) | find_ignored_lines(
            source, THIRD_PARTY_IGNORE_PATTERN
        )

        redundant = decide_candidates(
            get_session(), filepath, tree, source, level=self._level, ignored_lines=ignored_lines
        )

        return [
            Violation(
                check_id=CHECK_ID,
                error_code=ERROR_CODE,
                line=item.line,
                col=item.col,
                message=_format_message(item),
                fixable=False,
            )
            for item in redundant
        ]

    def fix(
        self,
        _filepath: Path,
        _violations: list[Violation],
        _source: str,
        _tree: ast.Module,
        _encoding: str = "utf-8",
    ) -> bool:
        """No autofix support in this version — see this package's own
        module docstring.
        """
        return False
