from collections.abc import Iterator


def takes_list(items: list[int]) -> int:
    return len(items)


def process(it: Iterator[int], value: int) -> str:
    takes_list(list(it))
    return str(value)
