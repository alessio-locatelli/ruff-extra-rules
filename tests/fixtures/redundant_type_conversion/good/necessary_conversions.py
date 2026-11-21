import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, overload


def takes_list(items: list[int]) -> int:
    return len(items)


def process(it: Iterator[int], value: int) -> str:
    takes_list(list(it))
    return str(value)


def joined(root: Path, name: str) -> str:
    return str(root / name)


def compared(root: Path, name: str, expected: list[str]) -> bool:
    return expected == [str(root / name)]


def subset_compared(expected: Any, performance_indexes: tuple[tuple[str, tuple[str, ...]], ...]) -> bool:
    return expected <= set(performance_indexes)


def dict_compared_as_a_set(executions: dict[str, str]) -> bool:
    return set(executions) == {"race:0", "race:1"}


class ApiZip:
    archive_path: str


def assign_a_converted_path_to_a_str_attribute(api_zip: ApiZip, archive_path: Path) -> None:
    api_zip.archive_path = str(archive_path)


class Logger:
    def warning(self, msg: str, /, **kwargs: object) -> None: ...


def log_a_path_argument(logger: Logger, a_path: Path) -> None:
    logger.warning("processing", path=str(a_path))


def serialize_a_set(missing: set[str]) -> str:
    return json.dumps(tuple(missing))


def print_a_set(missing: set[str]) -> None:
    print(tuple(missing))


def untyped_sink(value):
    return value


def pass_a_set_to_an_unannotated_parameter(missing: set[str]) -> None:
    untyped_sink(tuple(missing))


def describe(*, places: object | None = None) -> None: ...


def pass_a_set_by_keyword_to_an_object_parameter(missing: set[str]) -> None:
    describe(places=tuple(missing))


@overload
def consume(*, data: Iterable[str], flag: bool = False) -> None: ...
@overload
def consume(*, flag: bool = False, data: object) -> None: ...
def consume(*, data: object, flag: bool = False) -> None: ...


def pass_a_set_to_an_overload_that_accepts_anything(values: set[str]) -> None:
    consume(data=tuple(values))
