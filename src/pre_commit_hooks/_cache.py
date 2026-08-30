from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pre_commit_hooks._filelock import locked, locking_is_available

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["CacheManager"]

logger = logging.getLogger("cache")

_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_INTERVAL_SECONDS = 0.02


class CacheManager:
    __slots__ = ("_cache_dir_unavailable", "_locking_unavailable", "cache_dir", "cache_version", "hook_name")

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
        self._cache_dir_unavailable = False
        self._locking_unavailable = not locking_is_available()
        if self._locking_unavailable:
            logger.warning(
                "File locking is unavailable on this platform (os.name=%r); running without a cache, since "
                "concurrent hook runs could otherwise corrupt or lose cache entries without cross-process "
                "locking.",
                os.name,
            )
        self._ensure_cache_dir()

    @contextlib.contextmanager
    def _locked(self, cache_file: Path) -> Iterator[None]:
        lock_file = cache_file.with_suffix(".lock")
        with locked(
            lock_file, timeout_seconds=_LOCK_TIMEOUT_SECONDS, poll_interval_seconds=_LOCK_POLL_INTERVAL_SECONDS
        ):
            yield

    def _ensure_cache_dir(self) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

            tag_file = self.cache_dir / "CACHEDIR.TAG"
            if not tag_file.exists():
                tag_file.write_text(
                    "Signature: 8a477f597d28d172789f06886806bc55\n"
                    "# This directory is a cache directory for pre_commit_hooks.\n"
                    "# It is safe to delete this directory to clear the cache.\n"
                )

            if not os.access(self.cache_dir, os.W_OK):
                msg = f"{self.cache_dir} is not writable"
                raise PermissionError(msg)
        except OSError as error:
            logger.warning("Cache directory %s is unavailable, running without cache: %s", self.cache_dir, repr(error))
            self._cache_dir_unavailable = True

    def get_cached_result(self, filepath: Path, hook_name: str | None = None) -> dict[str, Any] | None:
        hook_name = hook_name or self.hook_name
        if self._cache_dir_unavailable or self._locking_unavailable:
            return None
        try:
            stat = filepath.stat()
            cache_file = self._get_cache_path(filepath)

            if not cache_file.exists():
                return None

            with self._locked(cache_file):
                with cache_file.open(encoding="utf-8") as f:
                    cache_data = json.load(f)

                if cache_data.get("version") != self.cache_version:
                    return None

                if cache_data.get("mtime") == stat.st_mtime_ns and cache_data.get("size") == stat.st_size:
                    return cache_data.get("hook_results", {}).get(hook_name)

                file_hash = self.compute_file_hash(filepath)
                if cache_data.get("file_hash") == file_hash:
                    cache_data["mtime"] = stat.st_mtime_ns
                    cache_data["size"] = stat.st_size
                    self._write_cache(cache_file, cache_data)
                    return cache_data.get("hook_results", {}).get(hook_name)

        except (OSError, json.JSONDecodeError, KeyError) as error:
            logger.warning("File: %s, hook name: %s, error: %s", filepath, hook_name, repr(error))
            return None
        else:
            return None

    def set_cached_result(self, filepath: Path, hook_name: str, hook_result: dict[str, Any]) -> None:
        if self._cache_dir_unavailable or self._locking_unavailable:
            return
        try:
            stat = filepath.stat()
            file_hash = self.compute_file_hash(filepath)
            cache_file = self._get_cache_path(filepath)

            with self._locked(cache_file):
                cache_data = None
                if cache_file.exists():
                    with cache_file.open(encoding="utf-8") as f:
                        cache_data = json.load(f)
                    if cache_data.get("version") != self.cache_version:
                        cache_data = None

                if cache_data is None:
                    cache_data = {"version": self.cache_version, "hook_results": {}}
                elif cache_data.get("file_hash") != file_hash:
                    cache_data["hook_results"] = {}

                cache_data["file_hash"] = file_hash
                cache_data["mtime"] = stat.st_mtime_ns
                cache_data["size"] = stat.st_size
                cache_data["hook_results"][hook_name] = hook_result
                cache_data["hook_results"][hook_name]["checked_at"] = int(time.time())

                self._write_cache(cache_file, cache_data)

        except (OSError, json.JSONDecodeError) as error:
            logger.warning("File: %s, hook name: %s, error: %s", filepath, hook_name, repr(error))

    def _get_cache_path(self, filepath: Path) -> Path:
        file_hash = hashlib.sha1(str(filepath.resolve()).encode(), usedforsecurity=False).hexdigest()
        cache_subdir = self.cache_dir / file_hash[:2]
        cache_subdir.mkdir(exist_ok=True)
        return cache_subdir / f"{file_hash}.json"

    @staticmethod
    def compute_file_hash(filepath: Path) -> str:
        """Returns SHA-1 hex digest."""
        sha1 = hashlib.sha1(usedforsecurity=False)
        with filepath.open("rb") as f:
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
        sha1 = hashlib.sha1(usedforsecurity=False)
        for py_file in sorted(root.rglob("*.py")):
            sha1.update(py_file.read_bytes())
        return sha1.hexdigest()

    def _write_cache(self, cache_file: Path, cache_data: dict[str, Any]) -> None:
        """Uses temp file + rename for atomic write on POSIX systems.

        The temp file comes from `tempfile.mkstemp()` rather than a fixed
        `<name>.tmp` sibling (matching `_base.py`'s `atomic_write_text()`,
        which needed the same hardening for source-file writes): a
        predictable path lets anyone who can write to `cache_dir` pre-plant
        a symlink there, which a plain `open(..., "w")` would silently
        follow -- writing cache content into whatever the symlink points at
        -- instead of creating a fresh file. `mkstemp()` creates the file
        itself, exclusively, so no pre-existing symlink at that name can
        ever exist to follow.
        """
        fd, temp_name = tempfile.mkstemp(dir=cache_file.parent, prefix=f".{cache_file.name}.", suffix=".tmp")
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, indent=2)
            temp_path.replace(cache_file)  # Atomic on POSIX
        finally:
            # Safety cleanup for error cases; temp file is atomically
            # renamed in success path, so this only runs on errors
            if temp_path.exists():
                temp_path.unlink()
