"""Which checks a given file opts out of.

See `docs/adr/0049-per-file-ignores.md`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from ._globs import glob_matches, relative_to_anchor

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["PerFileIgnore", "PerFileIgnoreList"]


@dataclass(frozen=True, slots=True)
class PerFileIgnore:
    """`pattern` never carries the leading `!` that sets `negated`."""

    pattern: str
    anchor: Path
    negated: bool
    check_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class PerFileIgnoreList:
    entries: tuple[PerFileIgnore, ...] = ()

    def ignored_check_ids(self, filepath: str | Path) -> frozenset[str]:
        """See `docs/adr/0049-per-file-ignores.md`."""
        if not self.entries:
            return frozenset()

        absolute = PurePosixPath(os.path.abspath(filepath))  # noqa: PTH100
        ignored: set[str] = set()
        for entry in self.entries:
            if _matches(entry, absolute) != entry.negated:
                ignored |= entry.check_ids
        return frozenset(ignored)


def _matches(entry: PerFileIgnore, absolute: PurePosixPath) -> bool:
    if glob_matches(entry.pattern, absolute.name):
        return True
    relative = relative_to_anchor(absolute, entry.anchor)
    return relative is not None and glob_matches(entry.pattern, str(relative))
