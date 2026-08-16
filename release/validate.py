from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pre_commit_hooks

from .changelog import UNRELEASED, parse
from .distribution import versions

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .changelog import Changelog

_TAG = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$")


def problems(
    *,
    distributions: Mapping[str, str],
    notes: Changelog,
    package_version: str,
    version: str,
) -> list[str]:
    found: list[str] = []
    if package_version != version:
        found.append(
            f"pre_commit_hooks.__version__ is {package_version}, but the tag names {version}; "
            "release the version the package declares."
        )
    if notes.latest.version != version:
        found.append(
            f"the newest changelog section is {notes.latest.version}, but the tag names {version}; "
            f"add a `## [{version}]` section describing what users are getting."
        )
    elif not notes.latest.body:
        found.append(f"the `## [{version}]` section is empty; describe what users are getting before releasing it.")
    if notes.unreleased:
        found.append(
            f"the `## [{UNRELEASED}]` section still holds notes; "
            f"move them into `## [{version}]` so the release ships them."
        )
    found.extend(
        f"{filename} reports version {built}, not {version}; rebuild the distribution from this tag."
        for filename, built in sorted(distributions.items())
        if built != version
    )
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tag")
    parser.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--notes-out", type=Path)
    arguments = parser.parse_args()

    tag = _TAG.match(arguments.tag)
    if tag is None:
        parser.error(f"{arguments.tag}: a release tag must look like v1.2.3.")
    version = tag["version"]

    try:
        notes = parse(arguments.changelog.read_text())
        distributions = versions(arguments.dist)
    except (OSError, ValueError) as error:
        parser.error(str(error))

    found = problems(
        distributions=distributions,
        notes=notes,
        package_version=pre_commit_hooks.__version__,
        version=version,
    )
    if found:
        for problem in found:
            print(problem, file=sys.stderr)
        return 1

    if arguments.notes_out is not None:
        try:
            arguments.notes_out.write_text(notes.notes(version) + "\n")
        except OSError as error:
            parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
