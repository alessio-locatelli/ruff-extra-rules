from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

from pre_commit_hooks.ast_checks.redundant_assignment import RedundantAssignmentCheck

if TYPE_CHECKING:
    from pre_commit_hooks.ast_checks._base import Violation


def _check(source: str, path: str = "test.py") -> list[Violation]:
    return RedundantAssignmentCheck().check(Path(path), ast.parse(source), source)
