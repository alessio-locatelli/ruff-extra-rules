"""Tests for validate_function_name hook (TRI004)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pre_commit_hooks.ast_checks.validate_function_name import ValidateFunctionNameCheck
from pre_commit_hooks.ast_checks.validate_function_name.analysis import (
    _call_name,
    analyze_function,
    attach_parents,
    decorator_name,
    is_decorator_override_or_abstract,
    process_file,
)


def _func(source: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(source)
    attach_parents(tree)
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )


def test_get_or_create_with_module_cache_not_flagged_as_mutation() -> None:
    """Regression case from .cache/bug_report.md: get_or_create updating a
    module-level cache must not be flagged as mutating its arguments.
    """
    source = """
import structlog
from structlog.typing import FilteringBoundLogger

logger: FilteringBoundLogger = structlog.getLogger("app")
_logger_per_query: dict[str, FilteringBoundLogger] = {}

def get_or_create_bound_logger(query) -> FilteringBoundLogger:
    '''Get or create a bound logger for a query.'''
    try:
        return _logger_per_query[query.id]
    except KeyError:
        log = logger.bind(depot_place_id=query.place_of_living)
        _logger_per_query[query.id] = log
        return log
"""

    tree = ast.parse(source)
    func_node = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "get_or_create_bound_logger"
    )

    analysis = analyze_function(func_node)

    assert not analysis["mutates_args"], (
        "get_or_create pattern with module cache should not be flagged as mutation"
    )


def test_get_with_argument_mutation_is_flagged() -> None:
    source = """
def get_users(database, filters):
    '''Get users and update filters dict.'''
    filters['processed'] = True  # Mutating argument!
    return database.query(filters)
"""

    tree = ast.parse(source)
    func_node = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "get_users"
    )

    analysis = analyze_function(func_node)

    assert analysis["mutates_args"], "Function mutating argument should be flagged"


def test_get_with_self_mutation_is_flagged() -> None:
    source = """
class Cache:
    def get_value(self, key):
        '''Get value and update internal state.'''
        value = self._cache.get(key)
        self.last_accessed = key  # Mutating self
        return value
"""

    tree = ast.parse(source)
    func_node = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "get_value"
    )

    analysis = analyze_function(func_node)

    assert analysis["mutates_args"], "Method mutating self should be flagged"


def test_get_with_argument_append_is_flagged() -> None:
    source = """
def get_items(container, new_item):
    '''Get items and append to container.'''
    container.append(new_item)  # Mutating argument!
    return container
"""

    tree = ast.parse(source)
    func_node = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "get_items"
    )

    from pre_commit_hooks.ast_checks.validate_function_name.analysis import (
        attach_parents,
    )

    attach_parents(tree)

    analysis = analyze_function(func_node)

    assert analysis["mutates_args"], "Function appending to argument should be flagged"


def test_get_with_local_list_append_not_flagged() -> None:
    source = """
def get_items(source):
    '''Get items from source.'''
    results = []
    for item in source:
        results.append(item)
    return results
"""

    tree = ast.parse(source)
    func_node = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "get_items"
    )

    from pre_commit_hooks.ast_checks.validate_function_name.analysis import (
        attach_parents,
    )

    attach_parents(tree)

    analysis = analyze_function(func_node)

    assert not analysis["mutates_args"], (
        "Function appending to local variable should not be flagged as mutation"
    )
    assert analysis["collects"], "Function should be flagged as collecting"


def test_get_with_augmented_assignment_to_param() -> None:
    source = """
def get_total(amount):
    '''Get total with tax added.'''
    amount += 10  # Modifying parameter (unusual but possible)
    return amount
"""

    tree = ast.parse(source)
    func_node = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "get_total"
    )

    analysis = analyze_function(func_node)

    assert analysis["mutates_args"], (
        "Augmented assignment to parameter should be flagged"
    )


def test_get_with_module_global_append_not_flagged() -> None:
    source = """
_cache = []

def get_cached_item(key):
    '''Get item from cache.'''
    _cache.append(key)  # Updating module global, not a parameter
    return _cache[-1]
"""

    tree = ast.parse(source)
    func_node = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "get_cached_item"
    )

    from pre_commit_hooks.ast_checks.validate_function_name.analysis import (
        attach_parents,
    )

    attach_parents(tree)

    analysis = analyze_function(func_node)

    assert not analysis["mutates_args"], (
        "Appending to module global should not be flagged as mutation"
    )


def test_process_file_with_get_or_create_cache_pattern(tmp_path: Path) -> None:
    source = """
_cache = {}

def get_or_create_item(key):
    '''Get or create an item in cache.'''
    if key not in _cache:
        _cache[key] = {"data": key}
    return _cache[key]
"""

    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    suggestions = process_file(filepath)

    for suggestion in suggestions:
        assert not suggestion.suggested_name.startswith("update_"), (
            f"Should not suggest update_ for cache pattern, "
            f"got: {suggestion.suggested_name}"
        )


def test_parameter_detection_with_all_arg_types() -> None:
    source = """
def get_data(regular, /, posonly, *args, kwonly=None, **kwargs):
    '''Get data and potentially mutate various param types.'''
    regular.update({"key": "value"})  # Should flag
    posonly.append(1)  # Should flag
    args[0] = "modified"  # Should flag (if possible)
    kwonly["key"] = "value"  # Should flag
    kwargs["key"] = "value"  # Should flag
    return None
"""

    tree = ast.parse(source)
    func_node = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "get_data"
    )

    from pre_commit_hooks.ast_checks.validate_function_name.analysis import (
        attach_parents,
    )

    attach_parents(tree)

    analysis = analyze_function(func_node)

    assert analysis["mutates_args"], (
        "Function mutating various parameter types should be flagged"
    )


def test_nested_attribute_access_mutation() -> None:
    source = """
def get_config(settings):
    '''Get config and update nested attributes.'''
    settings.database.connection_string = "new_value"
    return settings
"""

    tree = ast.parse(source)
    func_node = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "get_config"
    )

    analysis = analyze_function(func_node)

    # Limitation: only checks the first attribute level (settings.database, not deeper).
    assert analysis["mutates_args"], "Nested attribute mutation should be flagged"


def test_get_function_returning_class_is_not_flagged(tmp_path: Path) -> None:
    source = """
def get_placeholder_backend(original_exception):
    '''Create a placeholder backend class.'''
    class PlaceholderBackend:
        def __init__(*args, **kwargs):
            raise original_exception
    return PlaceholderBackend
"""

    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    suggestions = process_file(filepath)

    assert suggestions == [], "Functions returning classes should keep get_ prefix"


def test_docstring_verb_combine_detected(tmp_path: Path) -> None:
    source = """
def get_combined_revision(*functions):
    '''Combine the parameters of all revisions into a single revision.'''
    params = {}
    for func in functions:
        params.update(func.params)
    return params
"""

    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    suggestions = process_file(filepath)

    assert len(suggestions) == 1
    assert suggestions[0].suggested_name == "combine_combined_revision"
    assert "combine" in suggestions[0].reason.lower()


def test_mock_creation_suggests_create(tmp_path: Path) -> None:
    source = """
from unittest.mock import MagicMock

def get_mock_response(**kwargs):
    '''Get a mock response for testing.'''
    response_kwargs = {'url': 'http://test.com', 'status': 200}
    response_kwargs.update(kwargs)
    return MagicMock(spec=object, **response_kwargs)
"""

    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    suggestions = process_file(filepath)

    assert len(suggestions) == 1
    assert suggestions[0].suggested_name == "create_mock_response"
    assert "mock" in suggestions[0].reason.lower()


def test_async_get_function_is_flagged(tmp_path: Path) -> None:
    """Async get_* functions must be flagged, not just sync ones."""
    source = """
import requests

class Fetcher:
    async def get_api_data(self, url: str):
        '''Fetch data from API.'''
        return requests.get(url).json()
"""

    filepath = tmp_path / "source.py"
    filepath.write_text(source)

    suggestions = process_file(filepath)

    assert len(suggestions) == 1
    assert suggestions[0].func_name == "get_api_data"
    assert suggestions[0].suggested_name == "fetch_api_data"


def test_check_uses_given_tree_and_source_not_disk(tmp_path: Path) -> None:
    """check() must derive violations from the tree/source CheckOrchestrator
    hands it, not by independently re-reading the file from disk.

    The file on disk has no get_ functions at all; the tree/source passed to
    check() does. If check() ever regresses to re-reading the file itself
    (as it used to, via analysis.process_file), this would find zero
    violations instead of one.
    """
    filepath = tmp_path / "mod.py"
    filepath.write_text("x = 1\n")

    source = "def get_data() -> bool:\n    return True\n"
    tree = ast.parse(source)

    check = ValidateFunctionNameCheck()
    violations = check.check(filepath, tree, source)

    assert len(violations) == 1
    assert "get_data" in violations[0].message
    assert "is_data" in violations[0].message


def test_get_prefilter_pattern() -> None:
    assert ValidateFunctionNameCheck().get_prefilter_pattern() == ["def get_"]


def test_fix_with_no_violations_returns_false(tmp_path: Path) -> None:
    filepath = tmp_path / "mod.py"
    filepath.write_text("x = 1\n")
    tree = ast.parse("x = 1\n")

    check = ValidateFunctionNameCheck()
    assert check.fix(filepath, [], "x = 1\n", tree) is False


def test_fix_skips_violation_without_fix_data(tmp_path: Path) -> None:
    from pre_commit_hooks.ast_checks._base import Violation

    filepath = tmp_path / "mod.py"
    filepath.write_text("def get_data() -> bool:\n    return True\n")
    tree = ast.parse("x = 1\n")

    violation = Violation(
        check_id="validate-function-name",
        error_code="TRI004",
        line=1,
        col=0,
        message="unused",
        fixable=True,
        fix_data=None,
    )

    check = ValidateFunctionNameCheck()
    assert check.fix(filepath, [violation], "x = 1\n", tree) is False


def test_fix_skips_violation_without_suggestion_key(tmp_path: Path) -> None:
    from pre_commit_hooks.ast_checks._base import Violation

    filepath = tmp_path / "mod.py"
    filepath.write_text("def get_data() -> bool:\n    return True\n")
    tree = ast.parse("x = 1\n")

    violation = Violation(
        check_id="validate-function-name",
        error_code="TRI004",
        line=1,
        col=0,
        message="unused",
        fixable=True,
        fix_data={"other_key": 1},
    )

    check = ValidateFunctionNameCheck()
    assert check.fix(filepath, [violation], "x = 1\n", tree) is False


def test_fix_applies_safe_suggestion(tmp_path: Path) -> None:
    filepath = tmp_path / "mod.py"
    source = "def get_data() -> bool:\n    return True\n"
    filepath.write_text(source)
    tree = ast.parse(source)

    check = ValidateFunctionNameCheck()
    violations = check.check(filepath, tree, source)
    assert len(violations) == 1

    assert check.fix(filepath, violations, source, tree) is True
    assert violations[0].fix_data is not None
    assert violations[0].fix_data.get("fixed") is True
    assert "def is_data() -> bool:" in filepath.read_text()


def test_fix_skips_unsafe_suggestion(tmp_path: Path) -> None:
    """A suggestion should_autofix rejects (e.g. a method) isn't applied."""
    filepath = tmp_path / "mod.py"
    source = (
        "class Reader:\n"
        "    def get_data(self):\n"
        '        f = open("f.txt")\n'
        "        return f.read()\n"
    )
    filepath.write_text(source)
    tree = ast.parse(source)

    check = ValidateFunctionNameCheck()
    violations = check.check(filepath, tree, source)
    assert len(violations) == 1

    assert check.fix(filepath, violations, source, tree) is False
    assert filepath.read_text() == source


def test_fix_returns_false_when_apply_fix_fails_without_raising(
    tmp_path: Path,
) -> None:
    """apply_fix() can fail internally (e.g. a write error) and simply
    return False rather than raising; that must not be reported as fixed.

    The write goes through atomic_write_text's temp-file-then-rename, which
    only needs the parent directory to be writable (not the target file
    itself, since rename() doesn't check the destination's permission bits)
    — so the directory, not the file, has to be read-only to force a write
    failure here.
    """
    filepath = tmp_path / "mod.py"
    source = "def get_data() -> bool:\n    return True\n"
    filepath.write_text(source)
    tree = ast.parse(source)

    check = ValidateFunctionNameCheck()
    violations = check.check(filepath, tree, source)
    assert len(violations) == 1

    tmp_path.chmod(0o555)
    try:
        assert check.fix(filepath, violations, source, tree) is False
    finally:
        tmp_path.chmod(0o755)

    assert not (violations[0].fix_data and violations[0].fix_data.get("fixed"))


def test_fix_logs_and_continues_when_apply_fix_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pre_commit_hooks.ast_checks.validate_function_name as module

    filepath = tmp_path / "mod.py"
    source = "def get_data() -> bool:\n    return True\n"
    filepath.write_text(source)
    tree = ast.parse(source)

    check = ValidateFunctionNameCheck()
    violations = check.check(filepath, tree, source)
    assert len(violations) == 1

    def boom(*_args: object, **_kws: object) -> bool:
        raise RuntimeError("simulated apply_fix failure")

    monkeypatch.setattr(module, "apply_fix", boom)

    assert check.fix(filepath, violations, source, tree) is False


def test_call_name_returns_none_for_non_name_non_attribute_func() -> None:
    """A call whose func is neither a Name nor an Attribute (e.g. the result
    of another call, or a subscript) has no readable dotted name.
    """
    node = ast.parse("factory()()", mode="eval").body
    assert isinstance(node, ast.Call)
    assert _call_name(node.func) is None

    node2 = ast.parse("funcs[0]()", mode="eval").body
    assert isinstance(node2, ast.Call)
    assert _call_name(node2.func) is None


def test_decorator_name_from_call_returns_none() -> None:
    """A decorator that's a call whose own func isn't Name/Attribute (e.g.
    `@factory()()`) can't be resolved to a name.
    """
    source = "@factory()()\ndef get_data():\n    pass\n"
    tree = ast.parse(source)
    func_node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    assert decorator_name(func_node.decorator_list[0]) is None


def test_decorator_name_from_attribute() -> None:
    source = "@abc.abstractmethod\ndef get_data():\n    pass\n"
    tree = ast.parse(source)
    func_node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    assert decorator_name(func_node.decorator_list[0]) == "abc.abstractmethod"


def test_is_decorator_override_or_abstract_skips_unresolvable_decorator() -> None:
    """A decorator that isn't a Name/Attribute/resolvable Call is skipped
    (continue) rather than crashing, and doesn't count as override/abstract.
    """
    source = "@factory()()\ndef get_data():\n    pass\n"
    tree = ast.parse(source)
    func_node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    assert is_decorator_override_or_abstract(func_node) is False


def test_is_decorator_override_or_abstract_detects_attribute_form() -> None:
    source = "@abc.abstractmethod\ndef get_data():\n    pass\n"
    tree = ast.parse(source)
    func_node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    assert is_decorator_override_or_abstract(func_node) is True


def test_is_property_detected_via_attribute_decorator() -> None:
    func_node = _func(
        "class Foo:\n"
        "    @cached.property\n"
        "    def get_data(self):\n"
        "        return self._data\n",
        "get_data",
    )
    assert analyze_function(func_node)["is_property"] is True


def test_yields_flag_detected_for_generator() -> None:
    func_node = _func(
        "def get_items(items):\n    for item in items:\n        yield item\n",
        "get_items",
    )
    assert analyze_function(func_node)["yields"] is True


def test_disk_write_flag_detected() -> None:
    func_node = _func(
        "def get_data(path, content):\n"
        "    with open(path, 'w') as f:\n"
        "        f.write(content)\n",
        "get_data",
    )
    assert analyze_function(func_node)["disk_write"] is True


def test_parses_flag_detected_for_json_loads() -> None:
    func_node = _func("def get_data(raw):\n    return json.loads(raw)\n", "get_data")
    assert analyze_function(func_node)["parses"] is True


def test_renders_flag_detected_for_json_dumps() -> None:
    func_node = _func("def get_data(obj):\n    return json.dumps(obj)\n", "get_data")
    assert analyze_function(func_node)["renders"] is True


def test_network_write_flag_detected() -> None:
    func_node = _func(
        "def get_data(url, payload):\n    return httpx.post(url, payload)\n",
        "get_data",
    )
    analysis = analyze_function(func_node)
    assert analysis["network_write"] is True
    assert analysis["network_read"] is False


def test_network_call_matching_neither_verb_flags_neither() -> None:
    """A networking call whose verb isn't in the read or write verb lists
    (e.g. DELETE) doesn't set either flag.
    """
    func_node = _func("def get_data(url):\n    return httpx.delete(url)\n", "get_data")
    analysis = analyze_function(func_node)
    assert analysis["network_read"] is False
    assert analysis["network_write"] is False


def test_outputs_flag_detected_for_print() -> None:
    func_node = _func("def get_data(x):\n    print(x)\n    return x\n", "get_data")
    assert analyze_function(func_node)["outputs"] is True


def test_outputs_flag_detected_for_logger_call() -> None:
    func_node = _func(
        "def get_data(x):\n    logger.info(x)\n    return x\n", "get_data"
    )
    assert analyze_function(func_node)["outputs"] is True


def test_aggregates_flag_detected() -> None:
    func_node = _func("def get_total(values):\n    return sum(values)\n", "get_total")
    assert analyze_function(func_node)["aggregates"] is True


def test_searches_flag_detected() -> None:
    func_node = _func(
        "def get_index(items, target):\n    return items.index(target)\n",
        "get_index",
    )
    assert analyze_function(func_node)["searches"] is True


def test_validates_flag_detected() -> None:
    func_node = _func("def get_data(form):\n    return form.is_valid()\n", "get_data")
    assert analyze_function(func_node)["validates"] is True


def test_transforms_flag_detected() -> None:
    func_node = _func(
        "def get_data(items):\n    return items.transform()\n", "get_data"
    )
    assert analyze_function(func_node)["transforms"] is True


def test_delegates_get_flag_detected_via_assigned_variable() -> None:
    """Returning a variable that was assigned from a get_* call counts as
    delegation, same as returning the get_* call directly.
    """
    func_node = _func(
        "def get_wrapper(key):\n    value = get_value(key)\n    return value\n",
        "get_wrapper",
    )
    assert analyze_function(func_node)["delegates_get"] is True


def test_collects_flag_detected_for_list_call_container() -> None:
    func_node = _func(
        "def get_items(source):\n"
        "    items = list()\n"
        "    for x in source:\n"
        "        items.append(x)\n"
        "    return items\n",
        "get_items",
    )
    assert analyze_function(func_node)["collects"] is True


def test_mutates_args_false_for_non_param_attribute_target() -> None:
    """Assigning to an attribute of something that isn't a parameter (or
    self) isn't argument mutation.
    """
    func_node = _func(
        "def get_data():\n    some_module.CONFIG = {}\n    return 1\n",
        "get_data",
    )
    assert analyze_function(func_node)["mutates_args"] is False


def test_mutates_args_true_for_augmented_attribute_assignment() -> None:
    func_node = _func(
        "def get_data(arg):\n    arg.count += 1\n    return arg\n",
        "get_data",
    )
    assert analyze_function(func_node)["mutates_args"] is True


def test_mutates_args_false_for_augmented_attribute_on_non_param() -> None:
    func_node = _func(
        "def get_data():\n    counters.count += 1\n    return 1\n",
        "get_data",
    )
    assert analyze_function(func_node)["mutates_args"] is False


def test_mutates_args_true_for_augmented_name_param() -> None:
    func_node = _func(
        "def get_total(total):\n    total += 1\n    return total\n",
        "get_total",
    )
    assert analyze_function(func_node)["mutates_args"] is True


def test_mutates_args_false_for_augmented_local_variable() -> None:
    func_node = _func(
        "def get_total():\n    total = 0\n    total += 1\n    return total\n",
        "get_total",
    )
    assert analyze_function(func_node)["mutates_args"] is False


def test_searches_flag_detected_via_exists_loop() -> None:
    """A while-loop that calls .exists() (the find_root pattern) is treated
    as a search/find heuristic.
    """
    func_node = _func(
        "def get_root(path):\n"
        "    while not path.exists():\n"
        "        path = path.parent\n"
        "    return path\n",
        "get_root",
    )
    assert analyze_function(func_node)["searches"] is True


def test_validates_flag_detected_for_errors_variable() -> None:
    func_node = _func(
        "def get_data(form):\n    errors = []\n    return errors\n",
        "get_data",
    )
    assert analyze_function(func_node)["validates"] is True


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "validate_function_name"


@pytest.mark.parametrize(
    "fixture_path",
    sorted((FIXTURES_DIR / "bad").glob("*.py")),
    ids=lambda p: p.name,
)
def test_bad_fixtures_are_flagged(fixture_path: Path) -> None:
    assert process_file(fixture_path)


@pytest.mark.parametrize(
    "fixture_path",
    sorted((FIXTURES_DIR / "good").glob("*.py")),
    ids=lambda p: p.name,
)
def test_good_fixtures_are_not_flagged(fixture_path: Path) -> None:
    assert process_file(fixture_path) == []


@pytest.mark.parametrize(
    "fixture_path",
    sorted((FIXTURES_DIR / "ignore").glob("*.py")),
    ids=lambda p: p.name,
)
def test_ignore_fixtures_are_not_flagged(fixture_path: Path) -> None:
    assert process_file(fixture_path) == []


def test_decorator_name_from_name() -> None:
    source = "@override\ndef get_data():\n    pass\n"
    tree = ast.parse(source)
    func_node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    assert decorator_name(func_node.decorator_list[0]) == "override"


def test_decorator_name_from_attribute_unresolvable_base_returns_none() -> None:
    """An Attribute decorator whose base isn't a plain Name (e.g. the
    result of a call) can't be resolved to a dotted name.
    """
    source = "@get_deco().method\ndef get_data():\n    pass\n"
    tree = ast.parse(source)
    func_node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    assert decorator_name(func_node.decorator_list[0]) is None


def test_is_decorator_override_or_abstract_continues_past_non_matching_decorator() -> (
    None
):
    source = "@staticmethod\n@abc.abstractmethod\ndef get_data():\n    pass\n"
    tree = ast.parse(source)
    func_node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    assert is_decorator_override_or_abstract(func_node) is True


def test_is_property_false_for_non_property_decorator() -> None:
    func_node = _func(
        "class Foo:\n    @staticmethod\n    def get_data():\n        return 1\n",
        "get_data",
    )
    assert analyze_function(func_node)["is_property"] is False


def test_delegates_get_flag_detected_for_direct_return() -> None:
    func_node = _func("def get_wrapper():\n    return get_value()\n", "get_wrapper")
    assert analyze_function(func_node)["delegates_get"] is True


def test_delegation_tracking_skips_non_assign_parent() -> None:
    """Calling a get_* function directly in a return (not first assigned to
    a variable) doesn't register a delegation-tracked variable, though the
    direct-return check still detects the delegation itself.
    """
    func_node = _func(
        "def get_wrapper():\n    print(get_value())\n    return get_value()\n",
        "get_wrapper",
    )
    assert analyze_function(func_node)["delegates_get"] is True


def test_delegation_tracking_skips_non_name_assignment_target() -> None:
    """A tuple-unpacking assignment target isn't tracked as a delegated
    variable name.
    """
    func_node = _func(
        "def get_pair():\n    a, b = get_raw_pair()\n    return a\n",
        "get_pair",
    )
    # 'a' was never registered as a delegated var (target wasn't a plain
    # Name), so returning it isn't detected as delegation.
    assert analyze_function(func_node)["delegates_get"] is False


def test_mutation_detection_skips_unresolvable_call_name() -> None:
    """A mutation-verb call whose func isn't `obj.verb(...)` (e.g. a bare
    name call) isn't attributed to any variable.
    """
    func_node = _func("def get_data(x):\n    update(x)\n    return x\n", "get_data")
    assert analyze_function(func_node)["mutates_args"] is False


def test_exists_loop_scan_skips_non_exists_call() -> None:
    func_node = _func(
        "def get_value(counter):\n"
        "    while counter < 10:\n"
        "        counter = process(counter)\n"
        "    return counter\n",
        "get_value",
    )
    assert analyze_function(func_node)["searches"] is False


def test_returns_class_flag_detected_for_type_call() -> None:
    func_node = _func(
        "def get_class(name):\n    return type(name, (), {})\n", "get_class"
    )
    assert analyze_function(func_node)["returns_class"] is True


def test_get_base_name_returns_none_for_unsupported_expression() -> None:
    from pre_commit_hooks.ast_checks.validate_function_name.analysis import (
        _get_base_name,
    )

    node = ast.parse("a + b", mode="eval").body
    assert _get_base_name(node) is None


def test_is_simple_accessor_false_for_docstring_only_function() -> None:
    func_node = _func(
        'def get_data():\n    """Just a docstring, no return."""\n',
        "get_data",
    )
    from pre_commit_hooks.ast_checks.validate_function_name.analysis import (
        is_simple_accessor,
    )

    # No return statement at all, so nothing to suggest a rename for.
    assert is_simple_accessor(func_node) is False


def test_is_simple_accessor_false_for_non_return_single_statement() -> None:
    from pre_commit_hooks.ast_checks.validate_function_name.analysis import (
        is_simple_accessor,
    )

    func_node = _func("def get_data():\n    pass\n", "get_data")
    assert is_simple_accessor(func_node) is False


def test_is_simple_accessor_false_for_bare_return() -> None:
    from pre_commit_hooks.ast_checks.validate_function_name.analysis import (
        is_simple_accessor,
    )

    func_node = _func("def get_data():\n    return\n", "get_data")
    assert is_simple_accessor(func_node) is False


def test_is_simple_accessor_false_for_non_get_call_return() -> None:
    from pre_commit_hooks.ast_checks.validate_function_name.analysis import (
        is_simple_accessor,
    )

    func_node = _func("def get_data():\n    return compute_stuff()\n", "get_data")
    assert is_simple_accessor(func_node) is False


def test_process_file_read_error_returns_empty(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.py"
    assert process_file(missing) == []


def test_process_file_syntax_error_returns_empty(tmp_path: Path) -> None:
    filepath = tmp_path / "bad_syntax.py"
    filepath.write_text("def get_data(:\n")
    assert process_file(filepath) == []
