"""TR6: redundant builtin type-conversion calls. See
`docs/rules/redundant-type-conversion.md` and ADR-0035.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pre_commit_hooks.ast_checks._base import (
    BaseCheck,
    CheckResult,
    FixOutcome,
    FixResult,
    Violation,
    find_ignored_lines,
    find_ignored_lines_and_pytriage_comments,
    find_suppression_usage,
    ignore_pattern_for,
)
from pre_commit_hooks.ast_checks._options import EnumOption

from .analysis import decide_candidates
from .candidates import find_candidates
from .confidence import ConfidenceLevel, eligible_constructors, is_exact_match
from .session import get_session, peek_session, record_direct_input_if_session_active

if TYPE_CHECKING:
    import ast

    from pre_commit_hooks.ast_checks._options import CheckOption

IGNORE_PATTERN = ignore_pattern_for("TR6")

# See docs/rules/redundant-type-conversion.md's Suppression section.
THIRD_PARTY_IGNORE_PATTERN = re.compile(r"#\s*(?:type|pyright|ty)\s*:\s*ignore\b", re.IGNORECASE)

ERROR_CODE = "TR6"
CHECK_ID = "redundant-type-conversion"


def _cache_key(level: ConfidenceLevel, *, collect_suppression_usage: bool) -> str:
    return f"{level.name}:{'tracking' if collect_suppression_usage else 'normal'}"


def _format_message(constructor: str, argument_type: str) -> str:
    if is_exact_match(argument_type, constructor):
        return (
            f"Redundant `{constructor}(...)` conversion: the argument is already `{argument_type}`, so "
            f"wrapping it in `{constructor}()` has no effect. Or add '# pytriage: {ERROR_CODE}' to suppress."
        )
    # Not a real type match -- `ty` just didn't distinguish the two here
    # (e.g. either side of a weakly-typed `==`), see ADR-0035's permissive
    # hover-gate limitation. Saying "the argument is already X" would be
    # false when X (here `argument_type`) isn't actually `constructor`.
    return (
        f"Redundant `{constructor}(...)` conversion: `ty` sees no difference with or without this wrap here, "
        f"though the argument's own type is `{argument_type}`, not `{constructor}` -- verify before removing. "
        f"Or add '# pytriage: {ERROR_CODE}' to suppress."
    )


class RedundantTypeConversionCheck(BaseCheck):
    __slots__ = ("_level",)

    OPTIONS: ClassVar[tuple[CheckOption, ...]] = (
        EnumOption(
            name="level",
            values=ConfidenceLevel,
            default=ConfidenceLevel.CONSERVATIVE,
            help=(
                "How eagerly redundant-type-conversion (TR6) reports a violation. 'conservative' (default) "
                "flags str/int/float/bool/bytes/frozenset/tuple conversions, only when the wrapped value already "
                "matches that type exactly. 'permissive' also flags copy-producing conversions "
                "(list/dict/set/bytearray) and a looser match, e.g. an already-list[str] value passed somewhere "
                "only an Iterable[str] is required -- an explicit opt-in to the aliasing/mutation risk a "
                "copy-producing constructor's result can carry."
            ),
        ),
    )

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

    @property
    def tracks_direct_inputs(self) -> bool:
        """See ADR-0041."""
        return True

    def get_prefilter_pattern(self) -> list[str] | None:
        # Widens to "check every file" once a persistent daemon might already be running (ADR-0041): a
        # file can be a dependency of an already-tracked one without having any redundant-conversion
        # candidate of its own -- the narrow, candidate-name-based prefilter below would otherwise skip
        # that file and its direct-input lifecycle entirely, silently
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

    def check(self, filepath: Path, tree: ast.Module, source: str) -> CheckResult:
        return self._check(filepath, tree, source, collect_suppression_usage=False)

    def check_with_suppression_tracking(self, filepath: Path, tree: ast.Module, source: str) -> CheckResult:
        return self._check(filepath, tree, source, collect_suppression_usage=True)

    def _check(self, filepath: Path, tree: ast.Module, source: str, *, collect_suppression_usage: bool) -> CheckResult:
        candidates = find_candidates(tree, eligible_constructors(self._level))
        if not candidates:
            return CheckResult()

        if collect_suppression_usage:
            ignored_lines, format_suppressed, comments = find_ignored_lines_and_pytriage_comments(
                source, IGNORE_PATTERN, THIRD_PARTY_IGNORE_PATTERN
            )
            pytriage_lines = {
                comment.line
                for comment in comments
                if self.error_code in comment.codes and comment.line not in format_suppressed
            }
            analysis_ignored_lines = ignored_lines - pytriage_lines
        else:
            ignored_lines = find_ignored_lines(source, IGNORE_PATTERN, THIRD_PARTY_IGNORE_PATTERN)
            format_suppressed = set()
            comments = ()
            analysis_ignored_lines = ignored_lines
        if not any(candidate.line not in analysis_ignored_lines for candidate in candidates):
            return CheckResult()

        session = get_session()
        cache_key = _cache_key(self._level, collect_suppression_usage=collect_suppression_usage)
        redundancies = session.cached_redundancies(filepath, source, cache_key)
        if redundancies is None:
            redundant = decide_candidates(
                session, filepath, candidates, source, level=self._level, ignored_lines=analysis_ignored_lines
            )
            redundancies = [(item.candidate.constructor, item.line, item.col, item.argument_type) for item in redundant]
            session.cache_redundancies(filepath, source, cache_key, redundancies)

        violations = []
        suppression_usages = []
        for constructor, line, col, argument_type in redundancies:
            if line in ignored_lines:
                if collect_suppression_usage:
                    usage = find_suppression_usage(comments, format_suppressed, self.check_id, self.error_code, (line,))
                    if usage is not None:
                        suppression_usages.append(usage)
                continue
            violations.append(
                Violation(
                    check_id=CHECK_ID,
                    error_code=ERROR_CODE,
                    line=line,
                    col=col,
                    message=_format_message(constructor, argument_type),
                    fixable=False,
                )
            )
        return CheckResult(violations, suppression_usages)

    def record_direct_input(self, filepath: Path, source: str) -> None:
        record_direct_input_if_session_active(filepath, source)

    def fix(
        self,
        _filepath: Path,
        violations: list[Violation],
        _source: str,
        _tree: ast.Module,
        _encoding: str = "utf-8",
    ) -> FixResult:
        return FixResult.for_violations(violations, FixOutcome.DECLINED)

    def reconcile_direct_inputs(self, _direct_inputs: list[Path]) -> list[Path]:
        session = peek_session()
        if session is None:
            return []
        return session.reconcile_direct_inputs()
