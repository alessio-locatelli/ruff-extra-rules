"""TRI006: redundant builtin type-conversion calls. See
`docs/rules/redundant-type-conversion.md` and ADR-0035.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from pre_commit_hooks.ast_checks._base import BaseCheck, Violation, find_ignored_lines, ignore_pattern_for

from .analysis import RedundantConversion, decide_candidates
from .candidates import find_candidates
from .confidence import ConfidenceLevel, eligible_constructors, is_exact_match
from .session import get_session

if TYPE_CHECKING:
    import argparse
    import ast
    from pathlib import Path

# Format: # pytriage: ignore=TRI006
IGNORE_PATTERN = ignore_pattern_for("TRI006")

# See docs/rules/redundant-type-conversion.md's Suppression section.
THIRD_PARTY_IGNORE_PATTERN = re.compile(r"#\s*(?:type|pyright|ty)\s*:\s*ignore\b", re.IGNORECASE)

ERROR_CODE = "TRI006"
CHECK_ID = "redundant-type-conversion"


def _format_message(item: RedundantConversion) -> str:
    constructor = item.candidate.constructor
    if is_exact_match(item.argument_type, constructor):
        return (
            f"Redundant `{constructor}(...)` conversion: the argument is already `{item.argument_type}`, so "
            f"wrapping it in `{constructor}()` has no effect. Or add '# pytriage: ignore={ERROR_CODE}' to suppress."
        )
    # Not a real type match -- `ty` just didn't distinguish the two here
    # (e.g. either side of a weakly-typed `==`), see ADR-0035's permissive
    # hover-gate limitation. Saying "the argument is already X" would be
    # false when X (here `item.argument_type`) isn't actually `constructor`.
    return (
        f"Redundant `{constructor}(...)` conversion: `ty` sees no difference with or without this wrap here, "
        f"though the argument's own type is `{item.argument_type}`, not `{constructor}` -- verify before removing. "
        f"Or add '# pytriage: ignore={ERROR_CODE}' to suppress."
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
        """See ADR-0034."""
        return False

    def get_prefilter_pattern(self) -> list[str] | None:
        return [f"{name}(" for name in eligible_constructors(self._level)]

    @classmethod
    def add_cli_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--redundant-type-conversion-level",
            choices=["conservative", "permissive"],
            default="conservative",
            help=(
                "How eagerly redundant-type-conversion (TRI006) reports a violation. 'conservative' (default) "
                "flags str/int/float/bool/bytes/frozenset/tuple conversions, only when the wrapped value already "
                "matches that type exactly. 'permissive' also flags copy-producing conversions "
                "(list/dict/set/bytearray) and a looser match, e.g. an already-list[str] value passed somewhere "
                "only an Iterable[str] is required -- an explicit opt-in to the aliasing/mutation risk a "
                "copy-producing constructor's result can carry."
            ),
        )

    @classmethod
    def cli_kwargs_from_args(cls, args: argparse.Namespace) -> dict[str, Any]:
        return {"level": ConfidenceLevel[args.redundant_type_conversion_level.upper()]}

    def check(self, filepath: Path, tree: ast.Module, source: str) -> list[Violation]:
        ignored_lines = find_ignored_lines(source, IGNORE_PATTERN) | find_ignored_lines(
            source, THIRD_PARTY_IGNORE_PATTERN
        )

        # Checked before get_session() (which starts `ty` on this
        # process's first call): a prefilter match doesn't guarantee a
        # real candidate, and ignored_lines can suppress every real one.
        candidates = find_candidates(tree, eligible_constructors(self._level))
        if not any(candidate.line not in ignored_lines for candidate in candidates):
            return []

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
        return False
