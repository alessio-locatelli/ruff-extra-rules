from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from pre_commit_hooks.ast_checks.redundant_type_conversion.analysis import _DIAGNOSTICS_PROBE

if TYPE_CHECKING:
    from pathlib import Path

    from pre_commit_hooks.ast_checks.redundant_type_conversion.session import Redundancy

_DEFAULT_PROBE_DIAGNOSTICS = frozenset({("tr6-fake-probe", "fake session is trustworthy by default", 0, 0)})


class FakeSession:
    __slots__ = (
        "_diagnostics_by_content",
        "_hover_by_position",
        "_redundancies_by_content",
        "closed_files",
        "hover_calls",
        "opened_content",
        "within_root",
    )

    def __init__(
        self,
        *,
        diagnostics_by_content: dict[str, frozenset[tuple[object, ...]]],
        hover_by_position: dict[tuple[int, int], str | None],
        within_root: bool = True,
    ) -> None:
        self._diagnostics_by_content = diagnostics_by_content
        self._hover_by_position = hover_by_position
        self._redundancies_by_content: dict[tuple[str, str], list[Redundancy]] = {}
        self.opened_content: list[str] = []
        self.hover_calls: list[tuple[int, int]] = []
        self.closed_files: list[Path] = []
        self.within_root = within_root

    def is_within_root(self, _filepath: Path, /) -> bool:
        return self.within_root

    def open_or_update(self, _filepath: Path, content: str, /) -> frozenset[tuple[object, ...]]:
        self.opened_content.append(content)
        if content in self._diagnostics_by_content:
            return self._diagnostics_by_content[content]
        if content.endswith(_DIAGNOSTICS_PROBE):
            return _DEFAULT_PROBE_DIAGNOSTICS
        return frozenset()

    def hover(self, _filepath: Path, line0: int, char_utf16: int, /) -> str | None:
        self.hover_calls.append((line0, char_utf16))
        return self._hover_by_position.get((line0, char_utf16))

    def analysis_transaction(self) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()

    def finalize(self, filepath: Path, _source: str, /) -> None:
        self.closed_files.append(filepath)

    def cached_redundancies(self, _filepath: Path, source: str, cache_key: str, /) -> list[Redundancy] | None:
        return self._redundancies_by_content.get((source, cache_key))

    def cache_redundancies(
        self, _filepath: Path, source: str, cache_key: str, redundancies: list[Redundancy], /
    ) -> None:
        self._redundancies_by_content[(source, cache_key)] = redundancies
