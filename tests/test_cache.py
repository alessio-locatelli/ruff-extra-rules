"""Tests for _cache module."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pre_commit_hooks._cache import CacheManager


@pytest.fixture
def temp_cache_dir(tmp_path: Path) -> Path:
    """Create temporary cache directory."""
    cache_dir = tmp_path / ".cache"
    return cache_dir


@pytest.fixture
def cache_manager(temp_cache_dir: Path) -> CacheManager:
    """Create cache manager with temporary cache dir."""
    return CacheManager(cache_dir=temp_cache_dir, hook_name="test-hook")


@pytest.fixture
def sample_file(tmp_path: Path) -> Path:
    """Create sample Python file."""
    file_path = tmp_path / "sample.py"
    file_path.write_text("def foo():\n    pass\n")
    return file_path


def test_cache_dir_created(cache_manager: CacheManager, temp_cache_dir: Path) -> None:
    """Test that cache directory is created."""
    assert temp_cache_dir.exists()
    assert (temp_cache_dir / "CACHEDIR.TAG").exists()


def test_cachedir_tag_content(
    cache_manager: CacheManager, temp_cache_dir: Path
) -> None:
    """Test CACHEDIR.TAG has correct signature."""
    tag_content = (temp_cache_dir / "CACHEDIR.TAG").read_text()
    assert "Signature: 8a477f597d28d172789f06886806bc55" in tag_content


def test_cache_miss_on_first_access(
    cache_manager: CacheManager, sample_file: Path
) -> None:
    """Test that first access returns None (cache miss)."""
    result = cache_manager.get_cached_result(sample_file, "test-hook")
    assert result is None


def test_cache_hit_after_set(cache_manager: CacheManager, sample_file: Path) -> None:
    """Test that cached result is returned after setting."""
    test_result = {"violations": [], "checked_at": int(time.time())}

    # Set cache
    cache_manager.set_cached_result(sample_file, "test-hook", test_result)

    # Get cache
    cached = cache_manager.get_cached_result(sample_file, "test-hook")
    assert cached is not None
    assert cached["violations"] == []
    assert "checked_at" in cached


def test_get_cached_result_defaults_to_constructor_hook_name(
    cache_manager: CacheManager, sample_file: Path
) -> None:
    """hook_name is optional on get_cached_result: it falls back to the
    hook_name the CacheManager was constructed with (`cache_manager` fixture
    uses "test-hook")."""
    test_result: dict[str, list[str]] = {"violations": []}
    cache_manager.set_cached_result(sample_file, "test-hook", test_result)

    cached = cache_manager.get_cached_result(sample_file)
    assert cached is not None
    assert cached["violations"] == []


def test_cache_invalidated_on_content_change(
    cache_manager: CacheManager, sample_file: Path
) -> None:
    """Test that cache is invalidated when file content changes."""
    test_result: dict[str, list[str]] = {"violations": []}

    # Set cache
    cache_manager.set_cached_result(sample_file, "test-hook", test_result)
    assert cache_manager.get_cached_result(sample_file, "test-hook") is not None

    # Modify file content
    sample_file.write_text("def bar():\n    return 42\n")

    # Cache should be invalid
    cached = cache_manager.get_cached_result(sample_file, "test-hook")
    assert cached is None


def test_mtime_fast_path(cache_manager: CacheManager, sample_file: Path) -> None:
    """Test that mtime fast-path works (no hash computation on hit)."""
    test_result: dict[str, list[str]] = {"violations": []}

    # Set cache
    cache_manager.set_cached_result(sample_file, "test-hook", test_result)

    # Access cache without modifying file
    # This should use mtime fast-path (no hashing)
    cached = cache_manager.get_cached_result(sample_file, "test-hook")
    assert cached is not None


def test_mtime_changed_but_content_same(
    cache_manager: CacheManager, sample_file: Path
) -> None:
    """Test that cache is valid if mtime changed but content is same."""
    original_content = sample_file.read_text()
    test_result: dict[str, list[str]] = {"violations": []}

    # Set cache
    cache_manager.set_cached_result(sample_file, "test-hook", test_result)

    # Touch file (change mtime without changing content)
    time.sleep(0.01)  # Ensure mtime changes
    sample_file.write_text(original_content)

    # Cache should still be valid (content hash matches)
    cached = cache_manager.get_cached_result(sample_file, "test-hook")
    assert cached is not None


def test_multiple_hooks_same_file(
    cache_manager: CacheManager, sample_file: Path
) -> None:
    """Test that multiple hooks can cache results for same file."""
    result1 = {"violations": ["hook1"]}
    result2 = {"violations": ["hook2"]}

    cache_manager.set_cached_result(sample_file, "hook1", result1)
    cache_manager.set_cached_result(sample_file, "hook2", result2)

    cached1 = cache_manager.get_cached_result(sample_file, "hook1")
    cached2 = cache_manager.get_cached_result(sample_file, "hook2")

    assert cached1 is not None
    assert cached2 is not None
    assert cached1["violations"] == ["hook1"]
    assert cached2["violations"] == ["hook2"]


def test_cache_version_mismatch(temp_cache_dir: Path, sample_file: Path) -> None:
    """Test that cache is invalidated on version mismatch."""
    # Create cache with version 1.0.0
    cache_v1 = CacheManager(cache_dir=temp_cache_dir, cache_version="1.0.0")
    cache_v1.set_cached_result(sample_file, "test-hook", {"violations": []})

    # Try to read with version 2.0.0
    cache_v2 = CacheManager(cache_dir=temp_cache_dir, cache_version="2.0.0")
    cached = cache_v2.get_cached_result(sample_file, "test-hook")

    assert cached is None


def test_cache_version_mismatch_recovers_on_rewrite(
    temp_cache_dir: Path, sample_file: Path
) -> None:
    """A version bump must not pin a file to permanent cache misses.

    set_cached_result() used to load the on-disk blob (still tagged with
    the old version) and only patch individual keys, leaving the stale
    version tag in place forever — so every later run would keep missing
    and rewriting under the same never-updated old tag. Writing a fresh
    result under the new version must actually persist that version, so
    the immediately following read is a hit.
    """
    cache_v1 = CacheManager(cache_dir=temp_cache_dir, cache_version="1.0.0")
    cache_v1.set_cached_result(sample_file, "test-hook", {"violations": ["old"]})

    cache_v2 = CacheManager(cache_dir=temp_cache_dir, cache_version="2.0.0")
    assert cache_v2.get_cached_result(sample_file, "test-hook") is None

    cache_v2.set_cached_result(sample_file, "test-hook", {"violations": ["new"]})
    cached = cache_v2.get_cached_result(sample_file, "test-hook")

    assert cached is not None
    assert cached["violations"] == ["new"]


def test_compute_file_hash(sample_file: Path) -> None:
    """Test file hash computation."""
    hash1 = CacheManager.compute_file_hash(sample_file)
    assert len(hash1) == 40  # SHA-1 is 40 hex chars

    # Same file should produce same hash
    hash2 = CacheManager.compute_file_hash(sample_file)
    assert hash1 == hash2

    # Different content should produce different hash
    sample_file.write_text("different content")
    hash3 = CacheManager.compute_file_hash(sample_file)
    assert hash1 != hash3


def test_atomic_write(cache_manager: CacheManager, sample_file: Path) -> None:
    """Test that cache writes are atomic (no .tmp files left)."""
    test_result: dict[str, list[str]] = {"violations": []}
    cache_manager.set_cached_result(sample_file, "test-hook", test_result)

    # No .tmp files should exist
    tmp_files = list(cache_manager.cache_dir.rglob("*.tmp"))
    assert len(tmp_files) == 0


def test_cache_survives_read_error(
    cache_manager: CacheManager, temp_cache_dir: Path, sample_file: Path
) -> None:
    """Test that corrupted cache file is treated as cache miss."""
    # Create valid cache
    cache_manager.set_cached_result(sample_file, "test-hook", {"violations": []})

    # Corrupt cache file
    cache_path = cache_manager._get_cache_path(sample_file)
    cache_path.write_text("invalid json{")

    # Should return None (cache miss) instead of crashing
    cached = cache_manager.get_cached_result(sample_file, "test-hook")
    assert cached is None


def test_cache_survives_write_error(
    cache_manager: CacheManager, sample_file: Path, temp_cache_dir: Path
) -> None:
    """Test that write errors don't crash (graceful degradation)."""
    # Make cache directory read-only to trigger write error
    temp_cache_dir.chmod(0o444)

    # Should not crash
    try:
        cache_manager.set_cached_result(sample_file, "test-hook", {"violations": []})
    finally:
        # Restore permissions
        temp_cache_dir.chmod(0o755)


def test_clear_cache(cache_manager: CacheManager, sample_file: Path) -> None:
    """Test cache clearing."""
    # Create some cache entries
    cache_manager.set_cached_result(sample_file, "test-hook", {"violations": []})

    # Verify cache file exists
    cache_files = list(cache_manager.cache_dir.rglob("*.json"))
    assert len(cache_files) > 0

    # Clear old caches (older than 0 days = all)
    cache_manager.clear_cache(older_than_days=0)

    # All cache files should be gone
    cache_files = list(cache_manager.cache_dir.rglob("*.json"))
    assert len(cache_files) == 0

    # Test with files that are NOT old enough to clear
    cache_manager.set_cached_result(sample_file, "test-hook", {"violations": []})
    cache_files_before = list(cache_manager.cache_dir.rglob("*.json"))
    assert len(cache_files_before) > 0

    # Clear files older than 999 days (nothing should be deleted)
    cache_manager.clear_cache(older_than_days=999)

    # Files should still exist
    cache_files_after = list(cache_manager.cache_dir.rglob("*.json"))
    assert len(cache_files_after) == len(cache_files_before)


def test_cache_path_uses_two_level_structure(
    cache_manager: CacheManager, sample_file: Path
) -> None:
    """Test that cache uses two-level directory structure."""
    cache_path = cache_manager._get_cache_path(sample_file)

    # Should be in subdirectory (e.g., .cache/ab/abc123.json)
    assert cache_path.parent.parent == cache_manager.cache_dir
    assert len(cache_path.parent.name) == 2  # Two-char prefix


def test_different_files_different_cache_paths(
    cache_manager: CacheManager, tmp_path: Path
) -> None:
    """Test that different files get different cache paths."""
    file1 = tmp_path / "file1.py"
    file2 = tmp_path / "file2.py"
    file1.write_text("content1")
    file2.write_text("content2")

    path1 = cache_manager._get_cache_path(file1)
    path2 = cache_manager._get_cache_path(file2)

    assert path1 != path2


def test_concurrent_writers_do_not_lose_updates(
    cache_manager: CacheManager, sample_file: Path
) -> None:
    """Concurrent set_cached_result calls for different hook names on the same
    file must not clobber each other's entries.

    Regression: the read-modify-write of the shared per-file cache blob was
    unsynchronized, so under prek's parallel hook execution one writer's
    update could silently overwrite another's (lost update).
    """
    hook_names = [f"hook-{i}" for i in range(20)]

    with ThreadPoolExecutor(max_workers=len(hook_names)) as executor:
        futures = [
            executor.submit(
                cache_manager.set_cached_result, sample_file, name, {"value": name}
            )
            for name in hook_names
        ]
        for future in futures:
            future.result()

    for name in hook_names:
        cached = cache_manager.get_cached_result(sample_file, name)
        assert cached is not None, f"Lost update for {name!r}"
        assert cached["value"] == name
