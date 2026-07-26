from collections.abc import Iterable


def takes_list(items: list[int]) -> int:
    return len(items)


def takes_iterable(names: Iterable[str]) -> int:
    return sum(1 for _ in names)
