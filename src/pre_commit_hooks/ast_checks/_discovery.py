"""File discovery: turning the CLI's raw filename arguments into a concrete list of files to check."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("ast_checks")


def filter_excluded_files(filepaths: list[str], exclude_patterns: list[str]) -> list[str]:
    if not exclude_patterns:
        return filepaths

    filtered = []
    for filepath_str in filepaths:
        filepath = Path(filepath_str)
        excluded = False

        for pattern in exclude_patterns:
            if filepath.match(pattern):
                excluded = True
                break
            # Also match against each parent directory component.
            if any(part for part in filepath.parts if Path(part).match(pattern)):
                excluded = True
                break

        if not excluded:
            filtered.append(filepath_str)

    return filtered


def expand_directories(filenames: list[str]) -> list[str]:
    """Expand any directory argument into the `.py` files it contains.

    pre-commit/prek's own `types: [python]` hook contract (`.pre-commit-hooks.yaml`)
    always passes individual files, never a directory, so this only matters
    for direct CLI use (e.g. `ruff-extra-rules src/`, the form this
    project's own dev docs use — see AGENTS.md). Without it, a directory
    argument reached `CheckOrchestrator.process_files()` as a single
    unexpanded path: git grep's own directory pathspec support made it
    recurse and report matches for files *inside* that directory, but those
    resolved paths never matched the literal directory path in
    `git_grep_filter`'s own input map, so every result was silently
    discarded as unresolvable — the run reported zero violations, exit code
    0, having actually checked nothing.

    Each directory entry in `filenames` is replaced by the `.py` files found
    under it (see `_list_python_files_in_dir`); a non-directory entry (an
    ordinary file, or a path that doesn't exist at all) is kept as-is so the
    existing unreadable/unprocessable-file reporting still applies to it
    downstream.
    """
    expanded: list[str] = []
    for name in filenames:
        path = Path(name)
        if path.is_dir():
            expanded.extend(_list_python_files_in_dir(path))
        else:
            expanded.append(name)
    return expanded


def _list_python_files_in_dir(directory: Path) -> list[str]:
    """`.py` files under `directory`, as resolved absolute paths regardless
    of whether `directory` itself was given as a relative or absolute
    string — `git ls-files` always reports paths relative to whatever `-C`
    directory it was run against, so returning its output as-is would make
    a directory argument's expansion inconsistent with a plain file
    argument (passed through in whatever form the caller used); resolving
    both branches the same way keeps that consistent (ch. 13: "MUST handle
    relative and absolute paths consistently").

    Prefers `git ls-files --cached --others --exclude-standard` — tracked
    plus untracked-but-not-`.gitignore`d, so a brand-new file that hasn't
    been `git add`ed yet matches `git_grep_filter`'s own treatment of that
    same file when it's named explicitly instead of via its containing
    directory (ADR 0024) — and falls back to a plain recursive glob outside
    a git repo or when git itself is unavailable. A genuinely `.gitignore`d
    file is still excluded (avoids sweeping in `.venv`/build artifacts that
    happen to live under the given directory, ADR 0015), but
    `_warn_about_ignored_python_files` below reports that exclusion instead
    of leaving it silent (ADR 0028, issue #67).
    """
    resolved_dir = directory.resolve()
    try:
        cmd = ["git", "-C", str(directory), "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
        # cmd is built entirely from this function's own hardcoded git
        # subcommand/flags plus a directory supplied by this hook's own CLI
        # invocation (never from untrusted external input), so no shell is
        # involved and no argument here can inject another command.
        # errors="surrogateescape": paths are just bytes on Linux, never
        # required to be valid UTF-8 -- the default strict decoding would
        # otherwise raise UnicodeDecodeError for an oddly-encoded filename
        # and force falling all the way back to the untracked, non
        # `.gitignore`-aware rglob below for the *entire* directory, sweeping
        # in `.venv`/build artifacts to dodge one bad filename. surrogateescape
        # is the same handler `os.fsdecode()` already uses for filesystem
        # paths, so it never raises and round-trips back to the exact file
        # when resolved below.
        git_ls_files_result = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, errors="surrogateescape", check=False, timeout=30
        )
        if git_ls_files_result.returncode == 0 and not git_ls_files_result.stderr:
            # git ls-files reports the *index*, not the working tree: a
            # tracked file deleted from disk without `git rm` still shows up
            # here even though it no longer exists. A directory scan isn't
            # asking about that specific file by name (unlike an explicit
            # file argument, which git_grep_filter always still surfaces so
            # its own removal is reported) -- it's asking "what's currently
            # under here", so a stale index entry is silently dropped rather
            # than reported as a fake unreadable file (ch. 12: "MUST avoid
            # relying on stale file lists when the user explicitly requests
            # a current filesystem state").
            python_files = sorted(
                str(candidate)
                for f in git_ls_files_result.stdout.split("\0")
                if f.endswith(".py") and (candidate := resolved_dir / f).exists()
            )
            _warn_about_ignored_python_files(directory)
            return python_files
    except subprocess.SubprocessError, FileNotFoundError, subprocess.TimeoutExpired:
        # Self-healing: falls back to the equivalent rglob scan below.
        # Debug-only — an ERROR-level .exception() call here would leak a
        # raw traceback onto the user's stderr by default (nothing in this
        # codebase configures logging, so Python's own lastResort handler
        # prints WARNING+ straight to stderr) for a condition nothing
        # actually failed at from the user's perspective.
        logger.debug("git ls-files failed", exc_info=True)

    return sorted(str(p) for p in resolved_dir.rglob("*.py"))


_MAX_REPORTED_IGNORED_PATHS = 20

# Directory-shaped patterns from this project's own .gitignore: every one of
# these gets created by this project's own routine `mypy`/`pytest`/`build`/
# `uv sync` commands, so warning about them unconditionally fired on
# essentially every directory-argument run rather than the occasional case
# ADR 0028 anticipated (ADR 0029). None of these names are ever used for
# hand-written source, so skipping them costs nothing a directly-ignored
# `.py` file (still always reported below) wouldn't already catch.
_NON_SOURCE_DIRECTORY_NAMES = frozenset(
    {
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        "build",
        "develop-eggs",
        "dist",
        "downloads",
        "eggs",
        ".eggs",
        "lib",
        "lib64",
        "parts",
        "sdist",
        "var",
        "wheels",
        ".venv",
        "venv",
        "ENV",
        "env",
        "htmlcov",
        ".vscode",
        ".idea",
    }
)


def _is_known_non_source_directory(entry: str) -> bool:
    name = entry.rstrip("/").rpartition("/")[2]
    return name in _NON_SOURCE_DIRECTORY_NAMES or name.endswith(".egg-info")


def _warn_about_ignored_python_files(directory: Path) -> None:
    """Surfaces a directory scan's own `.gitignore`-driven exclusions
    instead of leaving them silent (issue #67), except for well-known
    non-source directories (ADR 0029) that this warning would otherwise
    report on nearly every run.

    `git status --porcelain=v1 --ignored` defaults to its "traditional"
    mode, which reports an entirely-ignored directory as a single `!! path/`
    line rather than recursing into it — unlike `git ls-files --others
    --ignored`, which would walk every file inside e.g. `.venv/`,
    reintroducing the sweep-in cost ADR 0015 avoided. That means this can't
    confirm an ignored directory actually contains a `.py` file without
    that same expensive recursion, so an ignored directory is reported
    alongside any directly-ignored `.py` file rather than silently
    dropped — unless its name matches `_NON_SOURCE_DIRECTORY_NAMES`, in
    which case it's dropped from the warning outright rather than reported
    on the strength of unconfirmed content.

    A directory that isn't *entirely* ignored (e.g. a generated/ subtree
    with one tracked README alongside thousands of individually-ignored
    `.py` outputs) can't collapse to one line even in traditional mode --
    git still has to report each ignored path separately, and this function
    has to buffer that whole reply before it can apply
    `_MAX_REPORTED_IGNORED_PATHS`'s own display cap below. This probe is
    purely supplementary (the directory scan above is already correct
    without it), so it gets a much shorter timeout than that scan's own git
    calls -- a slow git status here degrades to "no warning printed" rather
    than tying up an entire directory-argument run for a diagnostic that's
    allowed to just not fire.
    """
    try:
        cmd = [
            "git",
            "-C",
            str(directory),
            "status",
            "--porcelain=v1",
            "-z",
            "--ignored",
            # A user's own `status.showUntrackedFiles` config would otherwise
            # override this: "no" would suppress every `!!` record (the
            # warning this whole function exists for would just never fire),
            # while "all" would force recursion into an entirely-ignored
            # directory instead of the single collapsed line this function's
            # own cost reasoning above depends on. "normal" is git's own
            # built-in default and matches what every example above assumes.
            "--untracked-files=normal",
            "--",
            ".",
        ]
        # Same trust rationale as _list_python_files_in_dir's own git
        # invocation above: cmd is entirely hardcoded flags plus a directory
        # supplied by this hook's own CLI invocation. errors="surrogateescape"
        # for the same reason as that call's own subprocess.run above.
        git_status_result = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, errors="surrogateescape", check=False, timeout=5
        )
    except subprocess.SubprocessError, FileNotFoundError, subprocess.TimeoutExpired:
        # Self-healing, same rationale as _list_python_files_in_dir's own
        # except block above: this probe is purely supplementary, so any
        # failure just means no warning is printed.
        logger.debug("git status --ignored failed", exc_info=True)
        return

    if git_status_result.returncode != 0 or git_status_result.stderr:
        return

    ignored = [
        stripped
        for entry in git_status_result.stdout.split("\0")
        if entry.startswith("!! ")
        and (stripped := entry[len("!! ") :]).endswith((".py", "/"))
        and not (stripped.endswith("/") and _is_known_non_source_directory(stripped))
    ]
    if ignored:
        # Sorting only the capped slice, not the full (possibly huge) list,
        # keeps this bounded regardless of how many paths git reported.
        shown = sorted(ignored[:_MAX_REPORTED_IGNORED_PATHS])
        omitted_note = f" (showing first {len(shown)})" if len(ignored) > len(shown) else ""
        logger.warning(
            "%d gitignored path(s) under %s were excluded from this directory scan and may contain "
            ".py files these checks never examined; name a file explicitly on the command line to "
            "check it regardless of its ignore status%s: %s",
            len(ignored),
            directory,
            omitted_note,
            ", ".join(shown),
        )
