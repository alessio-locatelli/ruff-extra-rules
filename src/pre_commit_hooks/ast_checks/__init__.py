"""Grouped AST-based linter for pre-commit hooks.

This module provides a unified interface for running multiple AST-based checks
in a single pass, improving performance by eliminating redundant file I/O and
AST parsing operations.

Error Codes
-----------
  - TR1: Meaningless variable names (meaningless-vars)
  - TR2: Excessive blank lines (excessive-blank-lines)
  - TR3: Redundant super init (redundant-super-init)
  - TR4: Function naming violations (validate-function-name)
  - TR5: Redundant variable assignments (redundant-assignment)
  - TR6: Redundant builtin type conversions (redundant-type-conversion)
  - TR7: Comment misplaced on closing bracket line (misplaced-comment)
  - TR8: Unused pytriage suppression codes (unused-pytriage)

Inline Ignore Comments
----------------------
Use `# pytriage: <code>` to suppress a specific violation, or a
comma-separated list to suppress more than one on the same line.

Example:
    data = [1, 2, 3]  # pytriage: TR1
    def get_users():  # pytriage: TR4
        return []
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .excessive_blank_lines import ExcessiveBlankLinesCheck
from .meaningless_vars import MeaninglessVarsCheck
from .misplaced_comment import MisplacedCommentCheck
from .redundant_assignment import RedundantAssignmentCheck
from .redundant_super_init import RedundantSuperInitCheck
from .redundant_type_conversion import RedundantTypeConversionCheck
from .unused_pytriage import UnusedPytriageCheck
from .validate_function_name import ValidateFunctionNameCheck

if TYPE_CHECKING:
    from ._base import ASTCheck

# The complete, fixed set of checks the ruff-extra-rules hook can run. This
# package has no plugin mechanism for third-party checks, so a static list is
# all that's needed — add new checks here rather than via a registration
# side effect.
ALL_CHECKS: list[type[ASTCheck]] = [
    MeaninglessVarsCheck,
    ExcessiveBlankLinesCheck,
    RedundantSuperInitCheck,
    ValidateFunctionNameCheck,
    RedundantAssignmentCheck,
    MisplacedCommentCheck,
    RedundantTypeConversionCheck,
    UnusedPytriageCheck,
]
