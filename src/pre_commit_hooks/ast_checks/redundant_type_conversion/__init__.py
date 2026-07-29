"""TR6: redundant builtin type-conversion calls. See
`docs/rules/redundant-type-conversion.md` and ADR-0035.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pre_commit_hooks.ast_checks._base import BaseCheck, Violation, find_ignored_lines, ignore_pattern_for

from .analysis import RedundantConversion, decide_candidates
from .candidates import find_candidates
from .confidence import ConfidenceLevel, eligible_constructors, is_exact_match
from .session import get_session, notify_disk_change_if_session_active, peek_session

if TYPE_CHECKING:
    import argparse
    import ast

# Format: # pytriage: TR6
IGNORE_PATTERN = ignore_pattern_for("TR6")

# See docs/rules/redundant-type-conversion.md's Suppression section.
THIRD_PARTY_IGNORE_PATTERN = re.compile(r"#\s*(?:type|pyright|ty)\s*:\s*ignore\b", re.IGNORECASE)

ERROR_CODE = "TR6"
CHECK_ID = "redundant-type-conversion"


def _format_message(item: RedundantConversion) -> str:
    constructor = item.candidate.constructor
    if is_exact_match(item.argument_type, constructor):
        return (
            f"Redundant `{constructor}(...)` conversion: the argument is already `{item.argument_type}`, so "
            f"wrapping it in `{constructor}()` has no effect. Or add '# pytriage: {ERROR_CODE}' to suppress."
        )
    # Not a real type match -- `ty` just didn't distinguish the two here
    # (e.g. either side of a weakly-typed `==`), see ADR-0035's permissive
    # hover-gate limitation. Saying "the argument is already X" would be
    # false when X (here `item.argument_type`) isn't actually `constructor`.
    return (
        f"Redundant `{constructor}(...)` conversion: `ty` sees no difference with or without this wrap here, "
        f"though the argument's own type is `{item.argument_type}`, not `{constructor}` -- verify before removing. "
        f"Or add '# pytriage: {ERROR_CODE}' to suppress."
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
        # Widens to "check every file" once a persistent daemon might already be running (ADR-0041): a
        # file can be a dependency of an already-tracked one without having any redundant-conversion
        # candidate of its own -- the narrow, candidate-name-based prefilter below would otherwise skip
        # that file (and so its check(), and so notify_disk_change_if_session_active()) entirely, silently
        # reopening the exact cross-file gap this decision exists to close.
        #
        # Function-local: a module-level import here would make `daemon.py`
        # already be a cached submodule by the time `python -m
        # ...redundant_type_conversion.daemon` tries to run it as `__main__`
        # (this package's own `__init__.py` runs first), which trips a
        # `RuntimeWarning` and corrupts its stdout-based startup protocol.
        from . import daemon  # noqa: PLC0415

        if daemon.socket_exists_for(Path.cwd()):
            return None
        return [f"{name}(" for name in eligible_constructors(self._level)]

    @classmethod
    def add_cli_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--redundant-type-conversion-level",
            choices=["conservative", "permissive"],
            default="conservative",
            help=(
                "How eagerly redundant-type-conversion (TR6) reports a violation. 'conservative' (default) "
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
        # A prefilter match doesn't guarantee a real candidate: computed
        # before ever tokenizing `source` for ignored_lines (let alone
        # calling get_session(), which starts `ty` on this process's first
        # call) so a file with no syntactic candidates at all never pays
        # for either. It still needs notify_disk_change_if_session_active()
        # below: a pure signature change (this check's own cross-file
        # headline case, ADR-0041) has no candidate of its own, so this is
        # the only place such a file's change ever reaches an already-alive
        # session -- but it must never itself spawn one.
        candidates = find_candidates(tree, eligible_constructors(self._level))
        if not candidates:
            notify_disk_change_if_session_active(filepath, source)
            return []

        ignored_lines = find_ignored_lines(source, IGNORE_PATTERN, THIRD_PARTY_IGNORE_PATTERN)
        if not any(candidate.line not in ignored_lines for candidate in candidates):
            notify_disk_change_if_session_active(filepath, source)
            return []

        redundant = decide_candidates(
            get_session(), filepath, candidates, source, level=self._level, ignored_lines=ignored_lines
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

    def drain_cross_file_candidates(self, already_processed: list[Path]) -> list[Path]:
        """See ADR-0041. Peeks rather than calling `get_session()`: a run with no real candidate anywhere
        never created a session at all, and must not spawn one, or a daemon, just to ask it this.
        """
        session = peek_session()
        if session is None:
            return []
        return session.drain_cross_file_candidates(already_processed)
