from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pre_commit_hooks.ast_checks._base import FixOutcome
from pre_commit_hooks.ast_checks.redundant_super_init import RedundantSuperInitCheck

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "redundant_super_init"


def _check(source: str) -> list[str]:
    violations = RedundantSuperInitCheck().check(Path("test.py"), ast.parse(source), source)
    return [v.message for v in violations]


@pytest.mark.parametrize(
    "fixture_path",
    sorted((FIXTURES_DIR / "bad").glob("*.py")),
    ids=lambda p: p.name,
)
def test_bad_fixtures_are_flagged(fixture_path: Path) -> None:
    assert _check(fixture_path.read_text())


@pytest.mark.parametrize(
    "fixture_path",
    sorted((FIXTURES_DIR / "good").glob("*.py")),
    ids=lambda p: p.name,
)
def test_good_fixtures_are_not_flagged(fixture_path: Path) -> None:
    assert _check(fixture_path.read_text()) == []


@pytest.mark.parametrize(
    "fixture_path",
    sorted((FIXTURES_DIR / "ignore").glob("*.py")),
    ids=lambda p: p.name,
)
def test_ignore_fixtures_are_not_flagged(fixture_path: Path) -> None:
    assert _check(fixture_path.read_text()) == []


def test_check_id_and_error_code() -> None:
    check = RedundantSuperInitCheck()
    assert check.check_id == "redundant-super-init"
    assert check.error_code == "TR3"


def test_get_prefilter_pattern() -> None:
    assert RedundantSuperInitCheck().get_prefilter_pattern() == ["super().__init__"]


def test_fix_always_declines() -> None:
    source = (
        "class Base:\n    def __init__(self):\n        pass\n\n\n"
        "class Child(Base):\n    def __init__(self, **kwargs):\n        super().__init__(**kwargs)\n"
    )
    tree = ast.parse(source)
    check = RedundantSuperInitCheck()
    violations = check.check(Path("test.py"), tree, source)
    assert violations
    assert check.fix(Path("test.py"), violations, source, tree, "utf-8").outcomes == (FixOutcome.DECLINED,) * len(
        violations
    )


def test_violation_has_expected_line_and_no_fixable() -> None:
    source = """class Base:
    def __init__(self):
        pass


class Child(Base):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
"""
    violations = RedundantSuperInitCheck().check(Path("test.py"), ast.parse(source), source)

    assert len(violations) == 1
    violation = violations[0]
    assert violation.line == 7
    assert violation.col == 0
    assert violation.fixable is False
    assert "Base.__init__()" in violation.message


@pytest.mark.parametrize(
    ("second_fragment", "expected_lines"),
    [
        (
            (
                "class Second(Base):\n"
                "    def __init__(self, **kwargs):  # pytriage: TR3\n"
                "        super().__init__(**kwargs)\n"
            ),
            [7, 12],
        ),
        (
            (
                "# fmt: off\n"
                "class Second(Base):\n"
                "    def __init__(self, **kwargs):\n"
                "        super().__init__(**kwargs)\n"
                "# fmt: on\n"
            ),
            [7],
        ),
    ],
    ids=["pytriage", "format-suppressed"],
)
def test_check_records_suppression_usage_for_each_reportable_candidate(
    second_fragment: str, expected_lines: list[int]
) -> None:
    source = (
        "class Base:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "\n\n"
        "class First(Base):\n"
        "    def __init__(self, **kwargs):  # pytriage: TR3\n"
        "        super().__init__(**kwargs)\n"
        "\n\n"
        f"{second_fragment}"
    )

    check_result = RedundantSuperInitCheck().check(Path("test.py"), ast.parse(source), source)

    assert check_result == []
    assert [usage.line for usage in check_result.suppression_usages] == expected_lines


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        (
            """class Base:
    def __init__(self):
        pass


class Child(Base):
    def __init__(self, **kwargs):  # pytriage: TR3
        super().__init__(**kwargs)
""",
            False,
        ),
        (
            """class Base:
    def __init__(self):
        pass


class Child(Base):
    def __init__(self, value):
        self.value = value
        super().__init__()
""",
            False,
        ),
        ("class Foo:\n    pass\n", False),
        (
            """class Base:
    def __init__(self):
        pass


class Child(Base):
    def __init__(self, **kwargs):
        super().__init__()
        self.extra = kwargs
""",
            False,
        ),
        (
            """class Base:
    def __init__(self):
        pass


class Child(Base):
    def __init__(self, **kwargs):
        self.setup(**kwargs)

    def setup(self, **kwargs):
        pass
""",
            False,
        ),
        (
            """class Base:
    def __init__(self):
        pass


class Child(Base):
    def __init__(self, **kwargs):
        super().other_method(**kwargs)
""",
            False,
        ),
        (
            """class Base:
    def __init__(self):
        pass


class Child(Base):
    def __init__(self, **kwargs):
        super.__init__(**kwargs)
""",
            False,
        ),
        (
            """class Base:
    def __init__(self):
        pass


class Child(Base):
    def __init__(self, **kwargs):
        factory().__init__(**kwargs)
""",
            False,
        ),
        (
            """import external

class Child(external.Base):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
""",
            False,
        ),
        (
            """from somewhere import ExternalBase

class Child(ExternalBase):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
""",
            False,
        ),
        (
            """class Base:
    def __init__(self, name):
        self.name = name


class Child(Base):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
""",
            False,
        ),
        (
            """class Base:
    def __init__(self, *, name=None):
        self.name = name


class Child(Base):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
""",
            False,
        ),
        (
            """class Base:
    def __init__(self, value, /):
        self.value = value


class Child(Base):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
""",
            False,
        ),
        (
            """class Base:
    def __init__(self, /):
        pass


class Child(Base):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
""",
            True,
        ),
        (
            """class Child(Exception):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
""",
            False,
        ),
        (
            """class Child(BaseException):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
""",
            False,
        ),
    ],
    ids=[
        "inline-ignore-suppresses-violation",
        "init-without-kwargs-param",
        "class-without-init",
        "super-call-without-forwarding-kwargs",
        "non-super-call-in-init",
        "super-attr-not-named-init",
        "super-value-not-a-call",
        "func-value-not-super-name",
        "base-not-a-name-is-skipped",
        "unknown-external-base",
        "parent-accepts-positional-args-beyond-self",
        "parent-accepts-keyword-only-args",
        "parent-accepts-positional-only-args",
        "parent-self-only-positional-only-does-not-accept-args",
        "base-is-exception-accepts-kwargs-implicitly",
        "base-is-base-exception-accepts-kwargs-implicitly",
    ],
)
def test_check_flags_only_redundant_forwarding(source: str, *, flagged: bool) -> None:
    assert bool(_check(source)) is flagged


@pytest.mark.parametrize(
    ("source", "expected_substring"),
    [
        (
            """class GrandBase:
    def __init__(self):
        pass


class Middle(unresolved_module.SomeBase, GrandBase):
    pass


class Child(Middle):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
""",
            "Middle.__init__()",
        ),
        (
            """class Base1:
    def __init__(self):
        pass


class Base2:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class Child(Base1, Base2):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
""",
            "Base1.__init__()",
        ),
    ],
    ids=["recursive-parent-lookup-skips-non-name-base", "multiple-bases-only-one-flagged"],
)
def test_check_reports_single_violation_with_offending_base(source: str, expected_substring: str) -> None:
    violations = _check(source)

    assert len(violations) == 1
    assert expected_substring in violations[0]
