"""Grouped AST-based linter for pre-commit hooks.

This module provides a unified interface for running multiple AST-based checks
in a single pass, improving performance by eliminating redundant file I/O and
AST parsing operations.

Error Codes
-----------
  - TRI001: Forbid meaningless variable names (forbid-vars)
  - TRI002: Excessive blank lines (excessive-blank-lines)
  - TRI003: Redundant super init (redundant-super-init)
  - TRI004: Function naming violations (validate-function-name)
  - TRI005: Redundant variable assignments (redundant-assignment)
  - STYLE-001: Comment misplaced on closing bracket line (misplaced-comment)

Inline Ignore Comments
----------------------
Use `# pytriage: ignore=<code>` to suppress specific violations.

Example:
    data = [1, 2, 3]  # pytriage: ignore=TRI001
    def get_users():  # pytriage: ignore=TRI004
        return []
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .excessive_blank_lines import ExcessiveBlankLinesCheck
from .forbid_vars import ForbidVarsCheck
from .misplaced_comment import MisplacedCommentCheck
from .redundant_assignment import RedundantAssignmentCheck
from .redundant_super_init import RedundantSuperInitCheck
from .validate_function_name import ValidateFunctionNameCheck

if TYPE_CHECKING:
    from ._base import ASTCheck

# The complete, fixed set of checks the ruff-extra-rules hook can run. This
# package has no plugin mechanism for third-party checks, so a static list is
# all that's needed — add new checks here rather than via a registration
# side effect.
ALL_CHECKS: list[type[ASTCheck]] = [
    ForbidVarsCheck,
    ExcessiveBlankLinesCheck,
    RedundantSuperInitCheck,
    ValidateFunctionNameCheck,
    RedundantAssignmentCheck,
    MisplacedCommentCheck,
]
