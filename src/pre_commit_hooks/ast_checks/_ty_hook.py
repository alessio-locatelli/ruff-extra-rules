from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._base import ASTCheck

TY_HOOK_CHECK_IDS = frozenset({"redundant-dict-get", "redundant-type-conversion"})


def belongs_to_ty_hook(check_class: type[ASTCheck]) -> bool:
    return check_class().check_id in TY_HOOK_CHECK_IDS
