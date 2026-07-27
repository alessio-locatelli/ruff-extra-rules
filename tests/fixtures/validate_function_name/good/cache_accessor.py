"""Test fixture: cache-then-compute-on-miss accessors (should be skipped)."""

import json
from pathlib import Path


def get_cached_result(cache_path: Path, key: str):
    """Return a cached result from disk, computing nothing when it's missing."""
    if not cache_path.exists():
        return None
    try:
        with cache_path.open(encoding="utf-8") as f:
            cache_data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return cache_data.get(key)
