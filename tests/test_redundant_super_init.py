"""Tests for redundant_super_init hook (TRI003)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

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
    assert check.error_code == "TRI003"


def test_get_prefilter_pattern() -> None:
    assert RedundantSuperInitCheck().get_prefilter_pattern() == ["super().__init__"]


def test_fix_always_returns_false() -> None:
    source = "class Foo:\n    pass\n"
    tree = ast.parse(source)
    check = RedundantSuperInitCheck()
    violations = check.check(Path("test.py"), tree, source)
    assert check.fix(Path("test.py"), violations, source, tree, "utf-8") is False


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
    ("source", "flagged"),
    [
        (
            """class Base:
    def __init__(self):
        pass


class Child(Base):
    def __init__(self, **kwargs):  # pytriage: ignore=TRI003
        super().__init__(**kwargs)
""",
            False,
        ),
        (
            # No **kwargs parameter at all means nothing can be redundantly
            # forwarded.
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
            # super().__init__() called with no ** forwarding is never
            # flagged, even though the class itself accepts **kwargs.
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
            # A call that looks similar (e.g. self.setup(**kwargs)) but
            # isn't super().__init__ must not be mistaken for one.
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
            # super().other_method(**kwargs) isn't a super().__init__ call.
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
            # A bare `super.__init__(**kwargs)` (no call parens on `super`)
            # isn't the `super()` pattern this check looks for.
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
            # obj().__init__(**kwargs) where obj() isn't `super()` is not
            # flagged.
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
            # A base class expressed as something other than a plain Name
            # (e.g. an attribute access like `module.Base`) can't be
            # resolved, so it's skipped rather than flagged.
            """import external

class Child(external.Base):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
""",
            False,
        ),
        (
            # A base class not defined in this file (e.g. imported) can't
            # be introspected, so it's never flagged.
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
            # `self` alone as a positional-only parameter is not itself an
            # argument the caller can pass.
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
            # When an intermediate class (no __init__ of its own) has
            # multiple bases, a non-Name base (can't be resolved) is
            # skipped, and the search continues into the remaining bases
            # rather than stopping there.
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
            # Multiple inheritance: each base is checked independently,
            # and a violation is reported per non-accepting base.
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
