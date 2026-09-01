from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pre_commit_hooks.ast_checks._base import CheckResult, FixOutcome
from pre_commit_hooks.ast_checks._cli import main
from pre_commit_hooks.ast_checks.redundant_enum_value import RedundantEnumValueCheck


def _check(source: str) -> CheckResult:
    return RedundantEnumValueCheck().check(Path("test.py"), ast.parse(source), source)


@pytest.mark.parametrize(
    ("source", "expressions"),
    [
        (
            (
                "from enum import IntEnum, StrEnum, auto\n\n"
                "class Status(StrEnum):\n"
                "    READY = auto()\n"
                "    DONE = auto()\n\n"
                "class Code(IntEnum):\n"
                "    OK = 200\n\n"
                "first = Status.READY.value\n"
                "second = Status.DONE.value\n"
                "third = Code.OK.value\n"
            ),
            ["Status.READY.value", "Status.DONE.value", "Code.OK.value"],
        ),
        (
            ("from enum import IntEnum as Number\n\nclass Code(Number):\n    OK = 200\n\nvalue = Code.OK.value\n"),
            ["Code.OK.value"],
        ),
        (
            ("import enum\n\nclass State(enum.StrEnum):\n    OPEN = 'open'\n\nvalue = State.OPEN.value\n"),
            ["State.OPEN.value"],
        ),
        (
            "import enum\n\nclass State(enum.StrEnum):\n    OPEN = enum.auto()\n\nvalue = State.OPEN.value\n",
            ["State.OPEN.value"],
        ),
        (
            (
                "def build():\n"
                "    from enum import StrEnum as TextEnum\n\n"
                "    class State(TextEnum):\n"
                "        OPEN = 'open'\n\n"
                "    return State.OPEN.value\n"
            ),
            ["State.OPEN.value"],
        ),
        (
            ("from enum import StrEnum\n\nclass State(StrEnum):\n    OPEN = 'open'\n\nvalue: str = State.OPEN.value\n"),
            ["State.OPEN.value"],
        ),
        (
            (
                "from enum import StrEnum\n\n"
                "class State(StrEnum):\n"
                "    OPEN = 'open'\n\n"
                "class Container:\n"
                "    State = object\n\n"
                "    def read(self):\n"
                "        return State.OPEN.value\n"
            ),
            ["State.OPEN.value"],
        ),
    ],
    ids=[
        "str-enum-multiple-members",
        "aliased-int-enum",
        "qualified-str-enum",
        "qualified-auto",
        "function-local-enum",
        "annotated-runtime-value",
        "method-skips-class-binding",
    ],
)
def test_check_reports_direct_local_enum_member_values(source: str, expressions: list[str]) -> None:
    violations = _check(source)

    assert [source.splitlines()[violation.line - 1][violation.col :] for violation in violations] == expressions
    assert all(violation.fixable is False for violation in violations)
    assert all("pass the enum member directly" in violation.message for violation in violations)


@pytest.mark.parametrize(
    "source",
    [
        ("from enum import Enum\n\nclass Kind(Enum):\n    ITEM = 'item'\n\nvalue = Kind.ITEM.value\n"),
        "class Item:\n    value = 'item'\n\nvalue = Item.value\n",
        ("from package import StrEnum\n\nclass State(StrEnum):\n    OPEN = 'open'\n\nvalue = State.OPEN.value\n"),
        (
            "from enum import StrEnum\n\n"
            "class Base(StrEnum):\n"
            "    OPEN = 'open'\n\n"
            "class State(Base):\n"
            "    CLOSED = 'closed'\n\n"
            "value = State.CLOSED.value\n"
        ),
        ("from enum import StrEnum\n\nclass State(StrEnum):\n    OPEN = 'open'\n\nvalue = State.UNKNOWN.value\n"),
        (
            "from enum import StrEnum\n\n"
            "class State(StrEnum):\n"
            "    OPEN = 'open'\n"
            "    ALIAS = OPEN\n\n"
            "value = State.ALIAS.value\n"
        ),
        (
            "from enum import StrEnum\n\n"
            "class State(StrEnum):\n"
            "    OPEN = 'open'\n\n"
            "Alias = State\n"
            "value = Alias.OPEN.value\n"
        ),
        (
            "from enum import StrEnum\n\n"
            "class State(StrEnum):\n"
            "    OPEN = 'open'\n\n"
            "State = object\n"
            "value = State.OPEN.value\n"
        ),
        (
            "from enum import StrEnum\n\n"
            "class State(StrEnum):\n"
            "    OPEN = 'open'\n\n"
            "def read(State):\n"
            "    return State.OPEN.value\n"
        ),
        (
            "from enum import StrEnum, nonmember\n"
            "from types import SimpleNamespace\n\n"
            "class State(StrEnum):\n"
            "    OPEN = 'open'\n"
            "    METADATA = nonmember(SimpleNamespace(value='metadata'))\n\n"
            "value = State.METADATA.value\n"
        ),
        (
            "import enum\n"
            "from types import SimpleNamespace\n\n"
            "class State(enum.StrEnum):\n"
            "    OPEN = 'open'\n"
            "    METADATA = enum.nonmember(SimpleNamespace(value='metadata'))\n\n"
            "value = State.METADATA.value\n"
        ),
        (
            "from enum import StrEnum\n"
            "from types import SimpleNamespace\n\n"
            "def replace(_class):\n"
            "    return SimpleNamespace(OPEN=SimpleNamespace(value='open'))\n\n"
            "@replace\n"
            "class State(StrEnum):\n"
            "    OPEN = 'open'\n\n"
            "value = State.OPEN.value\n"
        ),
        (
            "from enum import StrEnum\n\n"
            "class Status(StrEnum):\n"
            "    READY = 'ready'\n\n"
            "    @property\n"
            "    def value(self):\n"
            "        return self.upper()\n\n"
            "value = Status.READY.value\n"
        ),
        (
            "from enum import StrEnum\n\n"
            "class Override:\n"
            "    @property\n"
            "    def value(self):\n"
            "        return self.upper()\n\n"
            "class State(Override, StrEnum):\n"
            "    OPEN = 'open'\n\n"
            "value = State.OPEN.value\n"
        ),
        (
            "from enum import StrEnum\n\n"
            "class Metadata:\n"
            "    value = 'metadata'\n\n"
            "class Descriptor:\n"
            "    def __get__(self, instance, owner):\n"
            "        return Metadata()\n\n"
            "class State(StrEnum):\n"
            "    OPEN = 'open'\n"
            "    METADATA = Descriptor()\n\n"
            "value = State.METADATA.value\n"
        ),
        (
            "from enum import StrEnum\n\n"
            "class Factory:\n"
            "    @staticmethod\n"
            "    def auto():\n"
            "        return 'metadata'\n\n"
            "class State(StrEnum):\n"
            "    OPEN = 'open'\n"
            "    METADATA = Factory.auto()\n\n"
            "value = State.METADATA.value\n"
        ),
        (
            "from enum import StrEnum\n\n"
            "class Factory:\n"
            "    metadata = 'metadata'\n\n"
            "class State(StrEnum):\n"
            "    OPEN = 'open'\n"
            "    METADATA = Factory.metadata\n\n"
            "value = State.METADATA.value\n"
        ),
        (
            "from enum import StrEnum\n"
            "from typing import Literal\n\n"
            "class State(StrEnum):\n"
            "    OPEN = 'open'\n\n"
            "value: Literal[State.OPEN.value]\n\n"
            "def read(*, value: Literal[State.OPEN.value]) -> Literal[State.OPEN.value]:\n"
            "    return value\n\n"
            "type StateValue = Literal[State.OPEN.value]\n"
        ),
        (
            "from enum import StrEnum\n\n"
            "class State:\n"
            "    class OPEN:\n"
            "        value = 'module'\n\n"
            "class Outer:\n"
            "    class State(StrEnum):\n"
            "        OPEN = 'outer'\n\n"
            "    class Inner:\n"
            "        value = State.OPEN.value\n"
        ),
        (
            "from enum import StrEnum\n\n"
            "class State(StrEnum):\n"
            "    def __new__(cls, value):\n"
            "        member = str.__new__(cls, value.upper())\n"
            "        member._value_ = value\n"
            "        return member\n\n"
            "    OPEN = 'open'\n\n"
            "value = State.OPEN.value\n"
        ),
        (
            "from enum import StrEnum\n\n"
            "class State(StrEnum):\n"
            "    OPEN = 'open'\n\n"
            "def read():\n"
            "    State = object\n"
            "    return State.OPEN.value\n"
        ),
        (
            "from enum import StrEnum\n\n"
            "class State(StrEnum):\n"
            "    OPEN = 'open'\n\n"
            "def read[State]():\n"
            "    return State.OPEN.value\n"
        ),
    ],
    ids=[
        "ordinary-enum",
        "ordinary-value-attribute",
        "imported-enum-base",
        "indirect-inheritance",
        "unknown-member",
        "member-alias",
        "class-alias",
        "class-rebinding",
        "parameter-shadowing",
        "nonmember-attribute",
        "qualified-nonmember-attribute",
        "decorated-enum-class",
        "value-override",
        "inherited-value-override",
        "descriptor-attribute",
        "unproven-qualified-member-value",
        "unproven-member-attribute",
        "type-annotation-and-alias",
        "nested-class-namespace",
        "custom-new",
        "local-shadowing",
        "type-parameter-shadowing",
    ],
)
def test_check_skips_unproven_enum_values(source: str) -> None:
    assert _check(source) == []


@pytest.mark.parametrize(
    ("source", "expected_usage_lines"),
    [
        (
            (
                "from enum import StrEnum\n\n"
                "class State(StrEnum):\n"
                "    OPEN = 'open'\n"
                "    CLOSED = 'closed'\n\n"
                "first = State.OPEN.value  # pytriage: TR10\n"
                "second = State.CLOSED.value\n"
            ),
            [7],
        ),
        (
            (
                "from enum import StrEnum\n\n"
                "class State(StrEnum):\n"
                "    OPEN = 'open'\n"
                "    CLOSED = 'closed'\n\n"
                "# fmt: off\n"
                "first = State.OPEN.value\n"
                "# fmt: on\n"
                "second = State.CLOSED.value\n"
            ),
            [],
        ),
    ],
    ids=["pytriage", "format-suppressed"],
)
def test_check_tracks_suppressed_enum_values(source: str, expected_usage_lines: list[int]) -> None:
    check_result = _check(source)

    assert len(check_result) == 1
    assert [usage.line for usage in check_result.suppression_usages] == expected_usage_lines


def test_check_covers_direct_members_and_expression_scopes() -> None:
    source = (
        "from enum import StrEnum\n"
        "import pathlib\n\n"
        "class State(StrEnum):\n"
        "    _INTERNAL = 'internal'\n"
        "    PENDING: str\n"
        "    OPEN: str = 'open'\n\n"
        "    def helper(self):\n"
        "        return self\n\n"
        "def read(default=State.OPEN.value):\n"
        "    return default\n\n"
        "async def async_read():\n"
        "    return State.OPEN.value\n\n"
        "callback_without_default = lambda: State.OPEN.value\n"
        "callback = lambda value=State.OPEN.value: value\n"
        "callback_required = lambda *, required: State.OPEN.value\n"
        "as_list = [State.OPEN.value for item in range(1)]\n"
        "as_set = {State.OPEN.value for item in range(1)}\n"
        "as_generator = (State.OPEN.value for item in range(1))\n"
        "as_dict = {item: State.OPEN.value for item in range(1)}\n"
    )

    assert len(_check(source)) == 9


def test_check_identity_prefilter_and_fix_contract() -> None:
    source = "from enum import IntEnum\n\nclass Status(IntEnum):\n    OK = 200\n\nvalue = Status.OK.value\n"
    tree = ast.parse(source)
    check = RedundantEnumValueCheck()
    violations = check.check(Path("test.py"), tree, source)

    assert check.check_id == "redundant-enum-value"
    assert check.error_code == "TR10"
    assert check.default_enabled is True
    assert check.cacheable is True
    assert check.get_prefilter_pattern() == [".value"]
    assert check.fix(Path("test.py"), violations, source, tree).outcomes == (FixOutcome.DECLINED,)


def test_cli_lists_selects_and_does_not_fix_redundant_enum_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    filepath = tmp_path / "module.py"
    source = "from enum import StrEnum\n\nclass Status(StrEnum):\n    OK = 'ok'\n\nvalue = Status.OK.value\n"
    filepath.write_text(source)

    assert main(["--list-checks"]) == 0
    assert "redundant-enum-value: TR10" in capsys.readouterr().out
    assert main(["--isolated", "--select", "redundant-enum-value", "--fix", str(filepath)]) == 1
    assert filepath.read_text() == source


def test_config_selects_redundant_enum_value(tmp_path: Path) -> None:
    filepath = tmp_path / "module.py"
    filepath.write_text(
        "from enum import StrEnum\n\nclass Status(StrEnum):\n    OK = 'ok'\n\nvalue = Status.OK.value\n"
    )
    config = tmp_path / "pyproject.toml"
    config.write_text('[tool.ruff-extra-rules]\nselect = ["redundant-enum-value"]\n')

    assert main(["--config", str(config), str(filepath)]) == 1
