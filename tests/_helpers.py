from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


def raises(exc_type: type[BaseException], message: str) -> Callable[..., NoReturn]:
    def _raise(*_args: object, **_kwargs: object) -> NoReturn:
        raise exc_type(message)

    return _raise


@contextlib.contextmanager
def restricted_permissions(path: Path, mode: int, *, restore: int) -> Iterator[None]:
    path.chmod(mode)
    try:
        yield
    finally:
        path.chmod(restore)
