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
