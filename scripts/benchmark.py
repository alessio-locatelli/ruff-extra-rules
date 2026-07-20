#!/usr/bin/env python3
"""Benchmark script to measure pre-commit hook performance.

Usage:
    python benchmark.py [--iterations=5] [--clear-cache]

This script measures:
- First run performance (cold cache)
- Incremental run performance (warm cache)
- Per-check breakdown
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import TypedDict

# Each entry runs one invocation of the real, currently-registered checks —
# all six now live behind the single ruff-extra-rules hook. The sub-checks
# are benchmarked individually via --select=<id>, plus one combined run
# mirroring .pre-commit-hooks.yaml's default args (every check enabled).
CHECKS: dict[str, list[str]] = {
    "ruff-extra-rules (all enabled)": [
        "python",
        "-m",
        "pre_commit_hooks.ast_checks",
    ],
    "forbid-vars": [
        "python",
        "-m",
        "pre_commit_hooks.ast_checks",
        "--select=forbid-vars",
    ],
    "excessive-blank-lines": [
        "python",
        "-m",
        "pre_commit_hooks.ast_checks",
        "--select=excessive-blank-lines",
    ],
    "redundant-super-init": [
        "python",
        "-m",
        "pre_commit_hooks.ast_checks",
        "--select=redundant-super-init",
    ],
    "validate-function-name": [
        "python",
        "-m",
        "pre_commit_hooks.ast_checks",
        "--select=validate-function-name",
    ],
    "redundant-assignment": [
        "python",
        "-m",
        "pre_commit_hooks.ast_checks",
        "--select=redundant-assignment",
    ],
    "misplaced-comment": [
        "python",
        "-m",
        "pre_commit_hooks.ast_checks",
        "--select=misplaced-comment",
    ],
}

CACHE_DIR = Path(".cache/pre_commit_hooks")


class CheckTimingResult(TypedDict):
    name: str
    elapsed_ms: float
    return_code: int
    files_checked: int


class BenchmarkIterationResult(TypedDict):
    label: str
    total_ms: float
    checks: list[CheckTimingResult]


def clear_cache() -> None:
    if CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR)
        print(f"✓ Cleared cache: {CACHE_DIR}")


def collect_source_and_test_files() -> list[str]:
    test_files = list(Path("tests").rglob("*.py"))
    src_files = list(Path("src").rglob("*.py"))
    return [str(f) for f in test_files + src_files]


def run_check(name: str, command: list[str], files: list[str]) -> CheckTimingResult:
    start = time.perf_counter()
    # command is one of this module's own hardcoded CHECKS entries and files
    # comes from local globbing in collect_source_and_test_files(), never
    # from untrusted external input, so no shell is involved and no argument
    # here can inject another command.
    result = subprocess.run(  # noqa: S603
        [*command, *files],
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = time.perf_counter() - start

    if result.returncode not in (0, 1):
        print(
            f"  ⚠ {name} exited {result.returncode}: {result.stderr.strip()[:200]}",
            file=sys.stderr,
        )

    return {
        "name": name,
        "elapsed_ms": elapsed * 1000,
        "return_code": result.returncode,
        "files_checked": len(files),
    }


def benchmark_iteration(files: list[str], label: str) -> BenchmarkIterationResult:
    print(f"\n{'=' * 60}")
    print(f"{label}")
    print(f"{'=' * 60}")

    results: list[CheckTimingResult] = []
    total_start = time.perf_counter()

    for name, command in CHECKS.items():
        result = run_check(name, command, files)
        results.append(result)
        print(f"  {name:30s} {result['elapsed_ms']:8.2f} ms ({result['files_checked']} files)")

    total_elapsed = time.perf_counter() - total_start

    print(f"{'-' * 60}")
    print(f"  {'Total':30s} {total_elapsed * 1000:8.2f} ms")

    return {
        "label": label,
        "total_ms": total_elapsed * 1000,
        "checks": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark pre-commit hooks")
    parser.add_argument(
        "--iterations",
        type=int,
        default=3,
        help="Number of iterations for each run type (default: 3)",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear cache before starting",
    )
    args = parser.parse_args()

    print("Pre-commit Hooks Performance Benchmark")
    print("=" * 60)

    files = collect_source_and_test_files()
    print(f"\nTest files: {len(files)} Python files")

    if args.clear_cache:
        clear_cache()

    all_results: list[BenchmarkIterationResult] = []

    # Run cold cache benchmarks
    print("\n\n📊 COLD CACHE (First Run) Benchmarks")
    print("=" * 60)
    cold_results = []
    for i in range(args.iterations):
        clear_cache()
        result = benchmark_iteration(files, f"Cold run {i + 1}/{args.iterations}")
        cold_results.append(result)
        all_results.append(result)

    # Run warm cache benchmarks
    print("\n\n📊 WARM CACHE (Incremental Run) Benchmarks")
    print("=" * 60)
    warm_results = []
    for i in range(args.iterations):
        result = benchmark_iteration(files, f"Warm run {i + 1}/{args.iterations}")
        warm_results.append(result)
        all_results.append(result)

    # Calculate averages
    print("\n\n" + "=" * 60)
    print("📈 SUMMARY")
    print("=" * 60)

    cold_avg = sum(r["total_ms"] for r in cold_results) / len(cold_results)
    warm_avg = sum(r["total_ms"] for r in warm_results) / len(warm_results)

    print(f"\nCold cache (first run):      {cold_avg:8.2f} ms")
    print(f"Warm cache (incremental):    {warm_avg:8.2f} ms")
    print(f"Cache speedup:               {(1 - warm_avg / cold_avg) * 100:7.1f}%")

    # Per-check averages
    print("\n" + "-" * 60)
    print("Per-check averages (cold cache):")
    print("-" * 60)

    for name in CHECKS:
        check_times = [next(c["elapsed_ms"] for c in r["checks"] if c["name"] == name) for r in cold_results]
        avg_time = sum(check_times) / len(check_times)
        print(f"  {name:30s} {avg_time:8.2f} ms")

    print("\n" + "-" * 60)
    print("Per-check averages (warm cache):")
    print("-" * 60)

    for name in CHECKS:
        check_times = [next(c["elapsed_ms"] for c in r["checks"] if c["name"] == name) for r in warm_results]
        avg_time = sum(check_times) / len(check_times)
        cold_time = sum(next(c["elapsed_ms"] for c in r["checks"] if c["name"] == name) for r in cold_results) / len(
            cold_results
        )
        speedup = (1 - avg_time / cold_time) * 100 if cold_time > 0 else 0
        print(f"  {name:30s} {avg_time:8.2f} ms ({speedup:+6.1f}%)")


if __name__ == "__main__":
    main()
