"""Tests for the ast_checks._base shared utilities."""

from __future__ import annotations

from pre_commit_hooks.ast_checks._base import byte_col_to_char_col


def test_byte_col_to_char_col_ascii_line_is_identity() -> None:
    line = "    data = calc()"
    assert byte_col_to_char_col(line, line.index("calc")) == line.index("calc")


def test_byte_col_to_char_col_converts_past_multibyte_char() -> None:
    line = '    x = "café"; return data'
    byte_offset = line.encode("utf-8").index(b"data")
    assert byte_col_to_char_col(line, byte_offset) == line.index("data")


def test_byte_col_to_char_col_converts_past_four_byte_char() -> None:
    line = "    x = '😀😀'; data = 1"
    byte_offset = line.encode("utf-8").index(b"data")
    assert byte_col_to_char_col(line, byte_offset) == line.index("data")
