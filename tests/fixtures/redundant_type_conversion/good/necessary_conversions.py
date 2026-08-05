from collections.abc import Iterator
from pathlib import Path


def takes_list(items: list[int]) -> int:
    return len(items)


def process(it: Iterator[int], value: int) -> str:
    takes_list(list(it))
    return str(value)


def joined(root: Path, name: str) -> str:
    return str(root / name)


def compared(root: Path, name: str, expected: list[str]) -> bool:
    return expected == [str(root / name)]
