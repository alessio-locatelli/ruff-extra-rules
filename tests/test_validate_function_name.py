"""Tests for validate_function_name hook (TRI004)."""

from __future__ import annotations

import ast
import tempfile
from pathlib import Path

from pre_commit_hooks.ast_checks.validate_function_name import ValidateFunctionNameCheck
from pre_commit_hooks.ast_checks.validate_function_name.analysis import (
    analyze_function,
    process_file,
)


def test_get_or_create_with_module_cache_not_flagged_as_mutation() -> None:
    """Test get_or_create with module-level cache is not flagged as mutation.

    This is the bug case from .cache/bug_report.md:
    - Function uses get_or_create pattern
    - Updates module-level cache (_logger_per_query)
    - Should NOT be flagged as mutation since it's not mutating arguments
    - Should keep get_ prefix (or suggest get_or_create_)
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
    func_node = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "get_or_create_bound_logger"
        ):
            func_node = node
            break

    assert func_node is not None, "Function not found in source"

    analysis = analyze_function(func_node)

    # The key assertion: should NOT be flagged as mutation
    # because _logger_per_query is NOT a parameter
    assert not analysis["mutates_args"], (
        "get_or_create pattern with module cache should not be flagged as mutation"
    )


def test_get_with_argument_mutation_is_flagged() -> None:
    """Test that functions mutating arguments ARE correctly flagged."""
    source = """
def get_users(database, filters):
    '''Get users and update filters dict.'''
    filters['processed'] = True  # Mutating argument!
    return database.query(filters)
"""

    tree = ast.parse(source)
    func_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "get_users":
            func_node = node
            break

    assert func_node is not None

    analysis = analyze_function(func_node)

    # Should be flagged as mutation since we're modifying the 'filters' parameter
    assert analysis["mutates_args"], "Function mutating argument should be flagged"


def test_get_with_self_mutation_is_flagged() -> None:
    """Test that methods mutating self ARE correctly flagged."""
    source = """
class Cache:
    def get_value(self, key):
        '''Get value and update internal state.'''
        value = self._cache.get(key)
        self.last_accessed = key  # Mutating self
        return value
"""

    tree = ast.parse(source)
    func_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "get_value":
            func_node = node
            break

    assert func_node is not None

    analysis = analyze_function(func_node)

    # Should be flagged as mutation since we're modifying self
    assert analysis["mutates_args"], "Method mutating self should be flagged"


def test_get_with_argument_append_is_flagged() -> None:
    """Test that appending to argument is flagged as mutation."""
    source = """
def get_items(container, new_item):
    '''Get items and append to container.'''
    container.append(new_item)  # Mutating argument!
    return container
"""

    tree = ast.parse(source)
    func_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "get_items":
            func_node = node
            break

    assert func_node is not None

    # Need to attach parents for proper analysis
    from pre_commit_hooks.ast_checks.validate_function_name.analysis import (
        attach_parents,
    )

    attach_parents(tree)

    analysis = analyze_function(func_node)

    # Should be flagged as mutation
    assert analysis["mutates_args"], "Function appending to argument should be flagged"


def test_get_with_local_list_append_not_flagged() -> None:
    """Test that appending to local variable is NOT flagged as mutation."""
    source = """
def get_items(source):
    '''Get items from source.'''
    results = []
    for item in source:
        results.append(item)
    return results
"""

    tree = ast.parse(source)
    func_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "get_items":
            func_node = node
            break

    assert func_node is not None

    from pre_commit_hooks.ast_checks.validate_function_name.analysis import (
        attach_parents,
    )

    attach_parents(tree)

    analysis = analyze_function(func_node)

    # Should NOT be flagged as mutation (results is local, not a parameter)
    # But should be flagged as "collects"
    assert not analysis["mutates_args"], (
        "Function appending to local variable should not be flagged as mutation"
    )
    assert analysis["collects"], "Function should be flagged as collecting"


def test_get_with_augmented_assignment_to_param() -> None:
    """Test that augmented assignment to parameter is flagged."""
    source = """
def get_total(amount):
    '''Get total with tax added.'''
    amount += 10  # Modifying parameter (unusual but possible)
    return amount
"""

    tree = ast.parse(source)
    func_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "get_total":
            func_node = node
            break

    assert func_node is not None

    analysis = analyze_function(func_node)

    # Should be flagged as mutation
    assert analysis["mutates_args"], (
        "Augmented assignment to parameter should be flagged"
    )


def test_get_with_module_global_append_not_flagged() -> None:
    """Test that appending to module-level global is NOT flagged as mutation."""
    source = """
_cache = []

def get_cached_item(key):
    '''Get item from cache.'''
    _cache.append(key)  # Updating module global, not a parameter
    return _cache[-1]
"""

    tree = ast.parse(source)
    func_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "get_cached_item":
            func_node = node
            break

    assert func_node is not None

    from pre_commit_hooks.ast_checks.validate_function_name.analysis import (
        attach_parents,
    )

    attach_parents(tree)

    analysis = analyze_function(func_node)

    # Should NOT be flagged as mutation (_cache is not a parameter)
    assert not analysis["mutates_args"], (
        "Appending to module global should not be flagged as mutation"
    )


def test_process_file_with_get_or_create_cache_pattern() -> None:
    """Integration: process_file should not suggest update_ for cache pattern."""
    source = """
_cache = {}

def get_or_create_item(key):
    '''Get or create an item in cache.'''
    if key not in _cache:
        _cache[key] = {"data": key}
    return _cache[key]
"""

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        f.flush()
        filepath = Path(f.name)

    try:
        suggestions = process_file(filepath)

        # Should not suggest renaming (or if it does, should NOT suggest update_)
        for suggestion in suggestions:
            assert not suggestion.suggested_name.startswith("update_"), (
                f"Should not suggest update_ for cache pattern, "
                f"got: {suggestion.suggested_name}"
            )
    finally:
        filepath.unlink()


def test_parameter_detection_with_all_arg_types() -> None:
    """Test that parameter detection works with all argument types."""
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
    func_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "get_data":
            func_node = node
            break

    assert func_node is not None

    from pre_commit_hooks.ast_checks.validate_function_name.analysis import (
        attach_parents,
    )

    attach_parents(tree)

    analysis = analyze_function(func_node)

    # Should be flagged as mutation (multiple param mutations)
    assert analysis["mutates_args"], (
        "Function mutating various parameter types should be flagged"
    )


def test_nested_attribute_access_mutation() -> None:
    """Test that nested attribute mutations on parameters are detected."""
    source = """
def get_config(settings):
    '''Get config and update nested attributes.'''
    settings.database.connection_string = "new_value"
    return settings
"""

    tree = ast.parse(source)
    func_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "get_config":
            func_node = node
            break

    assert func_node is not None

    analysis = analyze_function(func_node)

    # Note: current implementation only checks first level (settings.database)
    # This is a limitation but acceptable for the initial fix
    # Should be flagged since settings.database.connection_string starts with 'settings'
    assert analysis["mutates_args"], "Nested attribute mutation should be flagged"


def test_get_returning_class_not_flagged() -> None:
    """Test that functions returning classes keep get_ prefix."""
    source = """
def get_placeholder_backend(original_exception):
    '''Create a placeholder backend class.'''
    class PlaceholderBackend:
        def __init__(*args, **kwargs):
            raise original_exception
    return PlaceholderBackend
"""

    suggestions = []
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        f.flush()
        filepath = Path(f.name)

    try:
        suggestions = process_file(filepath)
    finally:
        filepath.unlink()

    # Should not suggest renaming because it returns a class
    assert len(suggestions) == 0, "Functions returning classes should keep get_ prefix"


def test_docstring_verb_combine_detected() -> None:
    """Test that 'combine' verb from docstring is used in suggestion."""
    source = """
def get_combined_revision(*functions):
    '''Combine the parameters of all revisions into a single revision.'''
    params = {}
    for func in functions:
        params.update(func.params)
    return params
"""

    suggestions = []
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        f.flush()
        filepath = Path(f.name)

    try:
        suggestions = process_file(filepath)
    finally:
        filepath.unlink()

    # Should suggest combine_ prefix based on docstring
    assert len(suggestions) == 1
    assert suggestions[0].suggested_name == "combine_combined_revision"
    assert "combine" in suggestions[0].reason.lower()


def test_mock_creation_suggests_create() -> None:
    """Test that mock/factory functions suggest create_ prefix."""
    source = """
from unittest.mock import MagicMock

def get_mock_response(**kwargs):
    '''Get a mock response for testing.'''
    response_kwargs = {'url': 'http://test.com', 'status': 200}
    response_kwargs.update(kwargs)
    return MagicMock(spec=object, **response_kwargs)
"""

    suggestions = []
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        f.flush()
        filepath = Path(f.name)

    try:
        suggestions = process_file(filepath)
    finally:
        filepath.unlink()

    # Should suggest create_ prefix for mock creation
    assert len(suggestions) == 1
    assert suggestions[0].suggested_name == "create_mock_response"
    assert "mock" in suggestions[0].reason.lower()


def test_async_get_function_is_flagged() -> None:
    """Async get_* functions must be flagged, not just sync ones."""
    source = """
import requests

class Fetcher:
    async def get_api_data(self, url: str):
        '''Fetch data from API.'''
        return requests.get(url).json()
"""

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        f.flush()
        filepath = Path(f.name)

    try:
        suggestions = process_file(filepath)
    finally:
        filepath.unlink()

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
