from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from . import ALL_CHECKS, RedundantDictGetCheck, RedundantTypeConversionCheck, UnusedPytriageCheck

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ._base import ASTCheck

MAIN_HOOK_ID = "ruff-extra-rules"
TY_HOOK_ID = "ruff-extra-rules-ty"

_TY_HOOK_ONLY_CHECKS = frozenset({RedundantDictGetCheck, RedundantTypeConversionCheck})

HOOK_CHECKS: Mapping[str, tuple[type[ASTCheck], ...]] = MappingProxyType(
    {
        MAIN_HOOK_ID: tuple(check_class for check_class in ALL_CHECKS if check_class not in _TY_HOOK_ONLY_CHECKS),
        TY_HOOK_ID: (
            *(check_class for check_class in ALL_CHECKS if check_class in _TY_HOOK_ONLY_CHECKS),
            UnusedPytriageCheck,
        ),
    }
)

HOOK_CHECK_IDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        hook_id: frozenset(check_class().check_id for check_class in check_classes)
        for hook_id, check_classes in HOOK_CHECKS.items()
    }
)
