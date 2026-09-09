from collections.abc import Iterator
from pathlib import Path
from typing import Any


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
