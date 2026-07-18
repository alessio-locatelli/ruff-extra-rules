"""File content hash caching for pre-commit hooks.

This module implements a content-hash-based cache similar to mypy's approach,
with mtime optimization for performance. Caches are stored in .cache/pre_commit_hooks/
and invalidated when file content changes.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["CacheManager"]

logger = logging.getLogger("cache")


class CacheManager:
    """Content-hash-based file cache with mtime optimization.

    Uses SHA-1 content hashing for cache keys with mtime fast-path optimization.
    Cache is stored in .cache/pre_commit_hooks/ directory in JSON format.

    `cache_version` has no default: a stale value here silently serves
    outdated results, so every caller must supply one that changes whenever
    something that could affect a cached result changes. See
    `ast_checks.CheckOrchestrator._generate_cache_key()` for the one real
    caller — it derives its version from a hash of its own source tree
    rather than a hand-maintained constant, which previously caused a real
    bug (commit 0e3efba) when a change was made without remembering to bump
    it.

    Example:
        >>> cache = CacheManager(hook_name="forbid-vars", cache_version="1")
        >>> result = cache.get_cached_result(Path("foo.py"))  # uses hook_name
        >>> if result is None:
        ...     # Run expensive check
        ...     violations = check_file("foo.py")
        ...     cache.set_cached_result(
        ...         Path("foo.py"), "forbid-vars", {"violations": violations}
        ...     )
    """

    DEFAULT_CACHE_DIR = Path(".cache/pre_commit_hooks")

    def __init__(
        self,
        cache_dir: Path | None = None,
        hook_name: str = "",
        *,
        cache_version: str,
    ) -> None:
        self.cache_dir = cache_dir or self.DEFAULT_CACHE_DIR
        self.hook_name = hook_name
        self.cache_version = cache_version
        self._ensure_cache_dir()

    @contextlib.contextmanager
    def _locked(self, cache_file: Path) -> Iterator[None]:
        """Hold an exclusive advisory lock while reading and rewriting a cache file.

        Multiple hook processes (e.g. under prek's parallel execution) can
        target the same per-file cache blob for different hook names at the
        same time. Without this lock, a read-modify-write race would let one
        process's write silently clobber another's (lost update).
        """
        lock_file = cache_file.with_suffix(".lock")
        with open(lock_file, "a", encoding="utf-8") as lock_fp:
            fcntl.flock(lock_fp, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_fp, fcntl.LOCK_UN)

    def _ensure_cache_dir(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Create CACHEDIR.TAG to mark this as a cache directory
        # See: https://bford.info/cachedir/
        tag_file = self.cache_dir / "CACHEDIR.TAG"
        if not tag_file.exists():
            tag_file.write_text(
                "Signature: 8a477f597d28d172789f06886806bc55\n"
                "# This directory is a cache directory for pre_commit_hooks.\n"
                "# It is safe to delete this directory to clear the cache.\n"
            )

    def get_cached_result(  # pytriage: ignore=TRI004
        self, filepath: Path, hook_name: str | None = None
    ) -> dict[str, Any] | None:
        """Uses mtime fast-path: if mtime unchanged, skip expensive hash computation.
        If mtime changed, verify with content hash.

        Args:
            filepath: File to look up
            hook_name: Hook whose cached result to fetch; defaults to the
                hook name this CacheManager was constructed with

        Returns:
            Cached result dict or None if cache invalid/missing
        """
        hook_name = hook_name or self.hook_name
        try:
            # Get file stats
            stat = filepath.stat()
            cache_file = self._get_cache_path(filepath)

            if not cache_file.exists():
                return None

            with self._locked(cache_file):
                # Load cache metadata
                with open(cache_file, encoding="utf-8") as f:
                    cache_data = json.load(f)

                # Version check
                if cache_data.get("version") != self.cache_version:
                    return None

                # Fast path: mtime + size check (no hashing needed)
                if (
                    cache_data.get("mtime") == stat.st_mtime_ns
                    and cache_data.get("size") == stat.st_size
                ):
                    # mtime unchanged, cache is valid!
                    return cache_data.get("hook_results", {}).get(hook_name)

                # Slow path: mtime changed, verify with content hash
                file_hash = self.compute_file_hash(filepath)
                if cache_data.get("file_hash") == file_hash:
                    # Content unchanged, update mtime in cache
                    cache_data["mtime"] = stat.st_mtime_ns
                    cache_data["size"] = stat.st_size
                    self._write_cache(cache_file, cache_data)
                    return cache_data.get("hook_results", {}).get(hook_name)

            # Content changed, cache invalid
            return None

        except (OSError, json.JSONDecodeError, KeyError) as error:
            logger.warning(
                "File: %s, hook name: %s, error: %s", filepath, hook_name, repr(error)
            )
            # Treat any error as cache miss
            return None

    def set_cached_result(
        self, filepath: Path, hook_name: str, hook_result: dict[str, Any]
    ) -> None:
        try:
            stat = filepath.stat()
            file_hash = self.compute_file_hash(filepath)
            cache_file = self._get_cache_path(filepath)

            with self._locked(cache_file):
                # Load existing cache or create new
                cache_data = None
                if cache_file.exists():
                    with open(cache_file, encoding="utf-8") as f:
                        cache_data = json.load(f)
                    if cache_data.get("version") != self.cache_version:
                        # Stale format/logic version: results under it may
                        # no longer be valid, so start fresh rather than
                        # silently keeping the old version tag on disk —
                        # that would pin this file to a permanent cache
                        # miss on every future run until .cache is
                        # manually cleared.
                        cache_data = None

                if cache_data is None:
                    cache_data = {"version": self.cache_version, "hook_results": {}}

                # Update cache
                cache_data["file_hash"] = file_hash
                cache_data["mtime"] = stat.st_mtime_ns
                cache_data["size"] = stat.st_size
                cache_data["hook_results"][hook_name] = hook_result
                cache_data["hook_results"][hook_name]["checked_at"] = int(time.time())

                # Atomic write
                self._write_cache(cache_file, cache_data)

        except (OSError, json.JSONDecodeError) as error:
            # Don't crash on cache write failure - just skip caching
            logger.warning(
                "File: %s, hook name: %s, error: %s", filepath, hook_name, repr(error)
            )

    def _get_cache_path(self, filepath: Path) -> Path:
        """Uses two-level directory structure for better filesystem performance:
        .cache/pre_commit_hooks/ab/abc123...def.json
        """
        # Hash the filepath (not content) to get stable cache location
        file_hash = hashlib.sha1(str(filepath.resolve()).encode()).hexdigest()
        cache_subdir = self.cache_dir / file_hash[:2]  # first 2 hex chars as prefix
        cache_subdir.mkdir(exist_ok=True)
        return cache_subdir / f"{file_hash}.json"

    @staticmethod
    def compute_file_hash(filepath: Path) -> str:
        """Returns SHA-1 hex digest."""
        sha1 = hashlib.sha1()
        with open(filepath, "rb") as f:
            # Read in 64KB chunks for large files
            for chunk in iter(lambda: f.read(65536), b""):
                sha1.update(chunk)
        return sha1.hexdigest()

    @staticmethod
    def compute_tree_hash(root: Path) -> str:
        """SHA-1 over every `.py` file's content under `root`, sorted for
        determinism. Recomputed fresh on every call rather than cached to
        disk itself — measured ~0.2ms for this repo's own src/ tree,
        negligible next to per-invocation interpreter startup.
        """
        sha1 = hashlib.sha1()
        for py_file in sorted(root.rglob("*.py")):
            sha1.update(py_file.read_bytes())
        return sha1.hexdigest()

    def _write_cache(self, cache_file: Path, cache_data: dict[str, Any]) -> None:
        """Uses temp file + rename for atomic write on POSIX systems."""
        temp_file = cache_file.with_suffix(".tmp")
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, indent=2)
            temp_file.replace(cache_file)  # Atomic on POSIX
        finally:
            # Safety cleanup for error cases; temp file is atomically
            # renamed in success path, so this only runs on errors
            if temp_file.exists():
                temp_file.unlink()
