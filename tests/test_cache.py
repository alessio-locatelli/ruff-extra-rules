from __future__ import annotations

import fcntl
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest

from pre_commit_hooks import _cache as cache_module
from pre_commit_hooks._cache import CacheManager

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@pytest.fixture
def temp_cache_dir(tmp_path: Path) -> Path:
    return tmp_path / ".cache"


@pytest.fixture
def cache_manager(temp_cache_dir: Path) -> CacheManager:
    return CacheManager(cache_dir=temp_cache_dir, hook_name="test-hook", cache_version="1")


@pytest.fixture
def sample_file(tmp_path: Path) -> Path:
    file_path = tmp_path / "sample.py"
    file_path.write_text("def foo():\n    pass\n")
    return file_path


@pytest.mark.usefixtures("cache_manager")
def test_cache_dir_created_with_cachedir_tag(temp_cache_dir: Path) -> None:
    tag = temp_cache_dir / "CACHEDIR.TAG"
    assert tag.exists()
    assert "Signature: 8a477f597d28d172789f06886806bc55" in tag.read_text()


def test_cache_miss_on_first_access(cache_manager: CacheManager, sample_file: Path) -> None:
    assert cache_manager.get_cached_result(sample_file, "test-hook") is None


@pytest.mark.parametrize(
    "call_args",
    [("test-hook",), ()],
    ids=["explicit-hook-name", "defaults-to-constructor-hook-name"],
)
def test_cache_hit_after_set(cache_manager: CacheManager, sample_file: Path, call_args: tuple[str, ...]) -> None:
    test_result = {"violations": [], "checked_at": int(time.time())}
    cache_manager.set_cached_result(sample_file, "test-hook", test_result)

    cached = cache_manager.get_cached_result(sample_file, *call_args)

    assert cached is not None
    assert cached["violations"] == []
    assert "checked_at" in cached


def test_cache_invalidated_on_content_change(cache_manager: CacheManager, sample_file: Path) -> None:
    test_result: dict[str, list[str]] = {"violations": []}

    cache_manager.set_cached_result(sample_file, "test-hook", test_result)
    assert cache_manager.get_cached_result(sample_file, "test-hook") is not None

    sample_file.write_text("def bar():\n    return 42\n")

    cached = cache_manager.get_cached_result(sample_file, "test-hook")
    assert cached is None


def test_mtime_changed_but_content_same(cache_manager: CacheManager, sample_file: Path) -> None:
    original_content = sample_file.read_text()
    test_result: dict[str, list[str]] = {"violations": []}

    cache_manager.set_cached_result(sample_file, "test-hook", test_result)

    time.sleep(0.01)  # ensure mtime changes
    sample_file.write_text(original_content)

    cached = cache_manager.get_cached_result(sample_file, "test-hook")
    assert cached is not None  # content hash still matches despite mtime change


def test_multiple_hooks_same_file(cache_manager: CacheManager, sample_file: Path) -> None:
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


def test_cache_version_mismatch_recovers_on_rewrite(temp_cache_dir: Path, sample_file: Path) -> None:
    # A version bump must not pin a file to permanent cache misses.
    # set_cached_result() used to load the on-disk blob (still tagged with
    # the old version) and only patch individual keys, leaving the stale
    # version tag in place forever — so every later run would keep missing
    # and rewriting under the same never-updated old tag. Writing a fresh
    # result under the new version must actually persist that version, so
    # the immediately following read is a hit.
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


@pytest.mark.parametrize(
    ("mutate", "changes"),
    [
        (lambda _: None, False),
        (lambda p: (p / "b.py").write_text("y = 3\n"), True),
        (lambda p: (p / "notes.txt").write_text("unrelated content"), False),
    ],
    ids=["unchanged-tree-is-stable", "python-file-change-changes-hash", "non-python-file-ignored"],
)
def test_compute_tree_hash(tmp_path: Path, mutate: Callable[[Path], object], *, changes: bool) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    hash1 = CacheManager.compute_tree_hash(tmp_path)

    mutate(tmp_path)
    hash2 = CacheManager.compute_tree_hash(tmp_path)

    assert (hash1 != hash2) is changes


def test_atomic_write(cache_manager: CacheManager, sample_file: Path) -> None:
    test_result: dict[str, list[str]] = {"violations": []}
    cache_manager.set_cached_result(sample_file, "test-hook", test_result)

    tmp_files = list(cache_manager.cache_dir.rglob("*.tmp"))
    assert len(tmp_files) == 0


def test_corrupted_cache_returns_miss_instead_of_crashing(cache_manager: CacheManager, sample_file: Path) -> None:
    cache_manager.set_cached_result(sample_file, "test-hook", {"violations": []})

    cache_path = cache_manager._get_cache_path(sample_file)
    cache_path.write_text("invalid json{")

    cached = cache_manager.get_cached_result(sample_file, "test-hook")
    assert cached is None


def test_cache_write_errors_do_not_crash(cache_manager: CacheManager, sample_file: Path, temp_cache_dir: Path) -> None:
    temp_cache_dir.chmod(0o444)  # read-only dir triggers a write error

    try:
        cache_manager.set_cached_result(sample_file, "test-hook", {"violations": []})
    finally:
        temp_cache_dir.chmod(0o755)


def test_construction_does_not_crash_when_cache_dir_is_unavailable(tmp_path: Path, sample_file: Path) -> None:
    # Regression: CacheManager.__init__ used to let mkdir()'s PermissionError
    # (or any other OSError creating/tagging the cache dir) propagate
    # uncaught, crashing the whole hook instead of degrading to uncached
    # execution. A read-only parent directory means the cache dir itself can
    # never be created.
    readonly_parent = tmp_path / "readonly_parent"
    readonly_parent.mkdir()
    readonly_parent.chmod(0o555)

    try:
        cache = CacheManager(cache_dir=readonly_parent / "cache", hook_name="test-hook", cache_version="1")

        assert cache.get_cached_result(sample_file, "test-hook") is None
        cache.set_cached_result(sample_file, "test-hook", {"violations": []})
        assert cache.get_cached_result(sample_file, "test-hook") is None
    finally:
        readonly_parent.chmod(0o755)


def test_construction_detects_pre_existing_read_only_cache_dir(tmp_path: Path) -> None:
    # Regression: a cache dir that already exists (from a prior run) with
    # its CACHEDIR.TAG already written makes both mkdir(exist_ok=True) and
    # the "if not tag_file.exists()" write-skip succeed without ever
    # attempting a write, so a directory later chmodded read-only (or
    # mounted read-only) looked exactly like an available one -- the
    # os.access(W_OK) check exists specifically to catch this case, which
    # neither a raised OSError nor a first write attempt would.
    existing_cache_dir = tmp_path / "cache"
    CacheManager(cache_dir=existing_cache_dir, hook_name="test-hook", cache_version="1")
    existing_cache_dir.chmod(0o555)

    try:
        cache = CacheManager(cache_dir=existing_cache_dir, hook_name="test-hook", cache_version="1")
        assert cache._cache_dir_unavailable is True
    finally:
        existing_cache_dir.chmod(0o755)


def test_unavailable_cache_dir_short_circuits_without_touching_filesystem(
    temp_cache_dir: Path, sample_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: once the cache directory is known unavailable, later calls
    # must not repeat the doomed mkdir() attempt (and its warning) per file
    # -- set_cached_result() used to hash the file's full content before
    # even reaching the failing mkdir(), wasted work on every processed file
    # for the rest of the run.
    cache = CacheManager(cache_dir=temp_cache_dir, hook_name="test-hook", cache_version="1")
    cache._cache_dir_unavailable = True

    def boom(*_args: object, **_kwargs: object) -> Path:
        raise AssertionError("_get_cache_path should not run once the cache dir is known unavailable")

    monkeypatch.setattr(CacheManager, "_get_cache_path", boom)

    assert cache.get_cached_result(sample_file, "test-hook") is None
    cache.set_cached_result(sample_file, "test-hook", {"violations": []})


def test_write_cache_cleans_up_temp_file_on_write_error(
    cache_manager: CacheManager,
) -> None:
    # The mkstemp-created temp file is removed even when writing to it fails
    # partway (e.g. non-JSON-serializable data), instead of being left
    # behind.
    cache_file = cache_manager.cache_dir / "some_hash.json"

    with pytest.raises(TypeError):
        cache_manager._write_cache(cache_file, {"bad": {1, 2, 3}})

    assert list(cache_manager.cache_dir.glob("*.tmp")) == []
    assert not cache_file.exists()


def test_write_cache_does_not_follow_a_symlink_planted_at_the_old_predictable_temp_name(
    cache_manager: CacheManager, tmp_path: Path
) -> None:
    # Regression: _write_cache() used to write through a fixed
    # `<hash>.tmp` sibling (`cache_file.with_suffix(".tmp")`), predictable
    # from the cache file's own name alone. Anyone able to write to
    # cache_dir could pre-plant a symlink at that exact path pointing at a
    # file the running user can write but doesn't intend to touch; a plain
    # `open(..., "w")` follows a symlink, so the write would land on the
    # symlink's target instead of a fresh file. Now that the temp file
    # comes from `tempfile.mkstemp()`, a pre-planted symlink at the old
    # fixed name is simply never used.
    cache_file = cache_manager.cache_dir / "some_hash.json"
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch\n")
    old_predictable_temp_path = cache_file.with_suffix(".tmp")
    old_predictable_temp_path.symlink_to(victim)

    cache_manager._write_cache(cache_file, {"hook_results": {}})

    assert victim.read_text() == "do not touch\n"
    assert cache_file.is_file()
    assert not cache_file.is_symlink()
    assert old_predictable_temp_path.is_symlink()  # untouched, still points at victim


def test_cache_path_uses_two_level_structure(cache_manager: CacheManager, sample_file: Path) -> None:
    cache_path = cache_manager._get_cache_path(sample_file)

    assert cache_path.parent.parent == cache_manager.cache_dir
    assert len(cache_path.parent.name) == 2  # two-char prefix


def test_different_files_different_cache_paths(cache_manager: CacheManager, tmp_path: Path) -> None:
    file1 = tmp_path / "file1.py"
    file2 = tmp_path / "file2.py"
    file1.write_text("content1")
    file2.write_text("content2")

    path1 = cache_manager._get_cache_path(file1)
    path2 = cache_manager._get_cache_path(file2)

    assert path1 != path2


def test_get_cached_result_degrades_to_cache_miss_when_lock_times_out(
    cache_manager: CacheManager, sample_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # fcntl.flock's own LOCK_EX blocks indefinitely, with no timeout. A
    # peer that's still holding the lock past _LOCK_TIMEOUT_SECONDS must
    # make this raise (and degrade to a cache miss) rather than hang.
    monkeypatch.setattr(cache_module, "_LOCK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(cache_module, "_LOCK_POLL_INTERVAL_SECONDS", 0.01)

    cache_manager.set_cached_result(sample_file, "test-hook", {"violations": []})
    lock_path = cache_manager._get_cache_path(sample_file).with_suffix(".lock")

    # A distinct file descriptor opened on the same path holds its own,
    # independent flock -- the same contention a second real process
    # opening the same lock file would cause.
    with lock_path.open("a", encoding="utf-8") as blocker_fp:
        fcntl.flock(blocker_fp, fcntl.LOCK_EX)

        start = time.monotonic()
        cached = cache_manager.get_cached_result(sample_file, "test-hook")
        elapsed = time.monotonic() - start

    assert cached is None
    assert elapsed < 2.0


def test_set_cached_result_does_not_hang_when_lock_times_out(
    cache_manager: CacheManager, sample_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cache_module, "_LOCK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(cache_module, "_LOCK_POLL_INTERVAL_SECONDS", 0.01)

    cache_manager.set_cached_result(sample_file, "test-hook", {"violations": []})
    lock_path = cache_manager._get_cache_path(sample_file).with_suffix(".lock")

    with lock_path.open("a", encoding="utf-8") as blocker_fp:
        fcntl.flock(blocker_fp, fcntl.LOCK_EX)

        start = time.monotonic()
        cache_manager.set_cached_result(sample_file, "test-hook", {"violations": ["new"]})
        elapsed = time.monotonic() - start

    assert elapsed < 2.0


def test_construction_warns_and_continues_when_fcntl_unavailable(
    temp_cache_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Windows has no fcntl module at all. `import fcntl` at module import
    # time used to make the whole package fail to import there before any
    # warning could even be printed (ch. 14: "MUST NOT hard-crash merely
    # because an optional platform feature is unavailable"). Simulated here
    # by monkeypatching the already-imported module-level name, the same
    # way test_main.py's SIGTERM tests simulate an unavailable platform
    # feature without a real Windows machine.
    monkeypatch.setattr(cache_module, "fcntl", None)

    with caplog.at_level(logging.WARNING, logger="cache"):
        cache = CacheManager(cache_dir=temp_cache_dir, hook_name="test-hook", cache_version="1")

    assert cache._locking_unavailable is True
    assert "fcntl" in caplog.text


def test_cache_disabled_entirely_without_fcntl(
    temp_cache_dir: Path, sample_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # _locked() exists specifically to stop two processes racing on the same
    # deterministic cache/temp-file path -- running unlocked would
    # reintroduce that exact race rather than degrade safely, so the whole
    # cache (not just locking) is disabled when fcntl is unavailable.
    monkeypatch.setattr(cache_module, "fcntl", None)
    cache = CacheManager(cache_dir=temp_cache_dir, hook_name="test-hook", cache_version="1")

    cache.set_cached_result(sample_file, "test-hook", {"violations": []})
    cached = cache.get_cached_result(sample_file, "test-hook")

    assert cached is None
    # No cache file was ever written, and no .lock file was ever opened --
    # _locked() (which would touch an fcntl API that isn't there) was never
    # even reached.
    assert list(cache.cache_dir.rglob("*.json")) == []
    assert list(cache.cache_dir.rglob("*.lock")) == []


def test_concurrent_writers_do_not_lose_updates(cache_manager: CacheManager, sample_file: Path) -> None:
    # Concurrent set_cached_result calls for different hook names on the
    # same file must not clobber each other's entries.
    #
    # Regression: the read-modify-write of the shared per-file cache blob
    # was unsynchronized, so under prek's parallel hook execution one
    # writer's update could silently overwrite another's (lost update).
    hook_names = [f"hook-{i}" for i in range(20)]

    with ThreadPoolExecutor(max_workers=len(hook_names)) as executor:
        futures = [
            executor.submit(cache_manager.set_cached_result, sample_file, name, {"value": name}) for name in hook_names
        ]
        for future in futures:
            future.result()

    for name in hook_names:
        cached = cache_manager.get_cached_result(sample_file, name)
        assert cached is not None, f"Lost update for {name!r}"
        assert cached["value"] == name
