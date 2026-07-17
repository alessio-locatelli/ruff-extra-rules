"""Tests for redundant_super_init hook (TRI003)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pre_commit_hooks.ast_checks.redundant_super_init import RedundantSuperInitCheck

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "redundant_super_init"


def _check(source: str) -> list[str]:
    tree = ast.parse(source)
    violations = RedundantSuperInitCheck().check(Path("test.py"), tree, source)
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
    tree = ast.parse(source)
    violations = RedundantSuperInitCheck().check(Path("test.py"), tree, source)

    assert len(violations) == 1
    violation = violations[0]
    assert violation.line == 7
    assert violation.col == 0
    assert violation.fixable is False
    assert "Base.__init__()" in violation.message


def test_inline_ignore_suppresses_violation() -> None:
    source = """class Base:
    def __init__(self):
        pass


class Child(Base):
    def __init__(self, **kwargs):  # pytriage: ignore=TRI003
        super().__init__(**kwargs)
"""
    assert _check(source) == []


def test_init_without_kwargs_param_not_flagged() -> None:
    """No **kwargs parameter at all means nothing can be redundantly forwarded."""
    source = """class Base:
    def __init__(self):
        pass


class Child(Base):
    def __init__(self, value):
        self.value = value
        super().__init__()
"""
    assert _check(source) == []


def test_class_without_init_not_flagged() -> None:
    source = "class Foo:\n    pass\n"
    assert _check(source) == []


def test_super_call_without_forwarding_kwargs_not_flagged() -> None:
    """super().__init__() called with no ** forwarding is never flagged,
    even though the class itself accepts **kwargs.
    """
    source = """class Base:
    def __init__(self):
        pass


class Child(Base):
    def __init__(self, **kwargs):
        super().__init__()
        self.extra = kwargs
"""
    assert _check(source) == []


def test_non_super_call_in_init_not_flagged() -> None:
    """A call that looks similar (e.g. self.setup(**kwargs)) but isn't
    super().__init__ must not be mistaken for one.
    """
    source = """class Base:
    def __init__(self):
        pass


class Child(Base):
    def __init__(self, **kwargs):
        self.setup(**kwargs)

    def setup(self, **kwargs):
        pass
"""
    assert _check(source) == []


def test_super_init_attr_not_named_init_not_flagged() -> None:
    """super().other_method(**kwargs) isn't a super().__init__ call."""
    source = """class Base:
    def __init__(self):
        pass


class Child(Base):
    def __init__(self, **kwargs):
        super().other_method(**kwargs)
"""
    assert _check(source) == []


def test_super_call_value_not_a_call_not_flagged() -> None:
    """A bare `super.__init__(**kwargs)` (no call parens on `super`) isn't
    the `super()` pattern this check looks for.
    """
    source = """class Base:
    def __init__(self):
        pass


class Child(Base):
    def __init__(self, **kwargs):
        super.__init__(**kwargs)
"""
    assert _check(source) == []


def test_call_whose_func_value_is_not_super_name_not_flagged() -> None:
    """obj().__init__(**kwargs) where obj() isn't `super()` is not flagged."""
    source = """class Base:
    def __init__(self):
        pass


class Child(Base):
    def __init__(self, **kwargs):
        factory().__init__(**kwargs)
"""
    assert _check(source) == []


def test_base_that_is_not_a_name_is_skipped() -> None:
    """A base class expressed as something other than a plain Name (e.g. an
    attribute access like `module.Base`) can't be resolved, so it's skipped
    rather than flagged.
    """
    source = """import external

class Child(external.Base):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
"""
    assert _check(source) == []


def test_unknown_external_base_not_flagged() -> None:
    """A base class not defined in this file (e.g. imported) can't be
    introspected, so it's never flagged.
    """
    source = """from somewhere import ExternalBase

class Child(ExternalBase):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
"""
    assert _check(source) == []


def test_parent_with_positional_args_beyond_self_accepts_args() -> None:
    source = """class Base:
    def __init__(self, name):
        self.name = name


class Child(Base):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
"""
    assert _check(source) == []


def test_parent_with_keyword_only_args_accepts_args() -> None:
    source = """class Base:
    def __init__(self, *, name=None):
        self.name = name


class Child(Base):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
"""
    assert _check(source) == []


def test_parent_with_positional_only_args_accepts_args() -> None:
    source = """class Base:
    def __init__(self, value, /):
        self.value = value


class Child(Base):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
"""
    assert _check(source) == []


def test_parent_with_only_self_positional_only_does_not_accept_args() -> None:
    """`self` alone as a positional-only parameter is not itself an
    argument the caller can pass.
    """
    source = """class Base:
    def __init__(self, /):
        pass


class Child(Base):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
"""
    assert _check(source)


def test_base_is_exception_accepts_kwargs_implicitly() -> None:
    source = """class Child(Exception):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
"""
    assert _check(source) == []


def test_base_is_base_exception_accepts_kwargs_implicitly() -> None:
    source = """class Child(BaseException):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
"""
    assert _check(source) == []


def test_recursive_parent_lookup_skips_non_name_base_and_keeps_checking() -> None:
    """When an intermediate class (no __init__ of its own) has multiple
    bases, a non-Name base (can't be resolved) is skipped, and the search
    continues into the remaining bases rather than stopping there.
    """
    source = """class GrandBase:
    def __init__(self):
        pass


class Middle(unresolved_module.SomeBase, GrandBase):
    pass


class Child(Middle):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
"""
    violations = _check(source)
    assert len(violations) == 1
    assert "Middle.__init__()" in violations[0]


def test_multiple_bases_only_one_flagged() -> None:
    """Multiple inheritance: each base is checked independently, and a
    violation is reported per non-accepting base.
    """
    source = """class Base1:
    def __init__(self):
        pass


class Base2:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class Child(Base1, Base2):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
"""
    violations = _check(source)
    assert len(violations) == 1
    assert "Base1.__init__()" in violations[0]
