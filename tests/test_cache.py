"""Tests for _cache module."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pre_commit_hooks._cache import CacheManager


@pytest.fixture
def temp_cache_dir(tmp_path: Path) -> Path:
    cache_dir = tmp_path / ".cache"
    return cache_dir


@pytest.fixture
def cache_manager(temp_cache_dir: Path) -> CacheManager:
    return CacheManager(cache_dir=temp_cache_dir, hook_name="test-hook")


@pytest.fixture
def sample_file(tmp_path: Path) -> Path:
    file_path = tmp_path / "sample.py"
    file_path.write_text("def foo():\n    pass\n")
    return file_path


@pytest.mark.usefixtures("cache_manager")
def test_cache_dir_created(temp_cache_dir: Path) -> None:
    assert temp_cache_dir.exists()
    assert (temp_cache_dir / "CACHEDIR.TAG").exists()


@pytest.mark.usefixtures("cache_manager")
def test_cachedir_tag_has_correct_signature(temp_cache_dir: Path) -> None:
    tag_content = (temp_cache_dir / "CACHEDIR.TAG").read_text()
    assert "Signature: 8a477f597d28d172789f06886806bc55" in tag_content


def test_cache_miss_on_first_access(
    cache_manager: CacheManager, sample_file: Path
) -> None:
    assert cache_manager.get_cached_result(sample_file, "test-hook") is None


def test_cache_hit_after_set(cache_manager: CacheManager, sample_file: Path) -> None:
    test_result = {"violations": [], "checked_at": int(time.time())}

    cache_manager.set_cached_result(sample_file, "test-hook", test_result)

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
    test_result: dict[str, list[str]] = {"violations": []}

    cache_manager.set_cached_result(sample_file, "test-hook", test_result)
    assert cache_manager.get_cached_result(sample_file, "test-hook") is not None

    sample_file.write_text("def bar():\n    return 42\n")

    cached = cache_manager.get_cached_result(sample_file, "test-hook")
    assert cached is None


def test_mtime_fast_path_skips_rehash_on_unmodified_file(
    cache_manager: CacheManager, sample_file: Path
) -> None:
    test_result: dict[str, list[str]] = {"violations": []}

    cache_manager.set_cached_result(sample_file, "test-hook", test_result)

    cached = cache_manager.get_cached_result(sample_file, "test-hook")
    assert cached is not None


def test_mtime_changed_but_content_same(
    cache_manager: CacheManager, sample_file: Path
) -> None:
    original_content = sample_file.read_text()
    test_result: dict[str, list[str]] = {"violations": []}

    cache_manager.set_cached_result(sample_file, "test-hook", test_result)

    time.sleep(0.01)  # ensure mtime changes
    sample_file.write_text(original_content)

    cached = cache_manager.get_cached_result(sample_file, "test-hook")
    assert cached is not None  # content hash still matches despite mtime change


def test_multiple_hooks_same_file(
    cache_manager: CacheManager, sample_file: Path
) -> None:
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
    cache_v1 = CacheManager(cache_dir=temp_cache_dir, cache_version="1.0.0")
    cache_v1.set_cached_result(sample_file, "test-hook", {"violations": []})

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
    hash1 = CacheManager.compute_file_hash(sample_file)
    assert len(hash1) == 40  # SHA-1 is 40 hex chars

    hash2 = CacheManager.compute_file_hash(sample_file)
    assert hash1 == hash2

    sample_file.write_text("different content")
    hash3 = CacheManager.compute_file_hash(sample_file)
    assert hash1 != hash3


def test_atomic_write(cache_manager: CacheManager, sample_file: Path) -> None:
    test_result: dict[str, list[str]] = {"violations": []}
    cache_manager.set_cached_result(sample_file, "test-hook", test_result)

    tmp_files = list(cache_manager.cache_dir.rglob("*.tmp"))
    assert len(tmp_files) == 0


def test_corrupted_cache_returns_miss_instead_of_crashing(
    cache_manager: CacheManager, sample_file: Path
) -> None:
    cache_manager.set_cached_result(sample_file, "test-hook", {"violations": []})

    cache_path = cache_manager._get_cache_path(sample_file)
    cache_path.write_text("invalid json{")

    cached = cache_manager.get_cached_result(sample_file, "test-hook")
    assert cached is None


def test_cache_write_errors_do_not_crash(
    cache_manager: CacheManager, sample_file: Path, temp_cache_dir: Path
) -> None:
    temp_cache_dir.chmod(0o444)  # read-only dir triggers a write error

    try:
        cache_manager.set_cached_result(sample_file, "test-hook", {"violations": []})
    finally:
        temp_cache_dir.chmod(0o755)


def test_write_cache_cleans_up_temp_file_on_write_error(
    cache_manager: CacheManager,
) -> None:
    """The .tmp file is removed even when writing to it fails partway
    (e.g. non-JSON-serializable data), instead of being left behind.
    """
    cache_file = cache_manager.cache_dir / "some_hash.json"

    with pytest.raises(TypeError):
        cache_manager._write_cache(cache_file, {"bad": {1, 2, 3}})

    assert not cache_file.with_suffix(".tmp").exists()
    assert not cache_file.exists()


def test_cache_path_uses_two_level_structure(
    cache_manager: CacheManager, sample_file: Path
) -> None:
    cache_path = cache_manager._get_cache_path(sample_file)

    assert cache_path.parent.parent == cache_manager.cache_dir
    assert len(cache_path.parent.name) == 2  # two-char prefix


def test_different_files_different_cache_paths(
    cache_manager: CacheManager, tmp_path: Path
) -> None:
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
