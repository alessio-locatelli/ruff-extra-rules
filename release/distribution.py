from __future__ import annotations

import email
import re
import tarfile
import zipfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

NAME = "ruff-extra-rules"


def _unreadable(path: Path) -> ValueError:
    return ValueError(f"{path}: the archive could not be read; rebuild the distribution with `uv build`.")


def _incomplete(path: Path, entry: str) -> ValueError:
    return ValueError(f"{path}: no readable {entry}; rebuild the distribution with `uv build`.")


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _version(payload: bytes, path: Path) -> str:
    metadata = email.message_from_bytes(payload)
    name = metadata.get("Name")
    if name is None or _normalized(name) != NAME:
        message = f"{path}: built for {name}, not {NAME}; build it from this project."
        raise ValueError(message)
    version = metadata.get("Version")
    if version is None:
        message = f"{path}: its metadata declares no Version; rebuild the distribution with `uv build`."
        raise ValueError(message)
    return version


def _wheel_version(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            names = [
                name for name in archive.namelist() if name.count("/") == 1 and name.endswith(".dist-info/METADATA")
            ]
            if len(names) != 1:
                raise _incomplete(path, "METADATA")
            payload = archive.read(names[0])
    except (OSError, zipfile.BadZipFile) as error:
        raise _unreadable(path) from error
    return _version(payload, path)


def _sdist_version(path: Path) -> str:
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = [
                member
                for member in archive.getmembers()
                if member.name.count("/") == 1 and member.name.endswith("/PKG-INFO")
            ]
            if len(members) != 1:
                raise _incomplete(path, "PKG-INFO")
            extracted = archive.extractfile(members[0])
            if extracted is None:
                raise _incomplete(path, "PKG-INFO")
            payload = extracted.read()
    except (OSError, tarfile.TarError) as error:
        raise _unreadable(path) from error
    return _version(payload, path)


def versions(directory: Path) -> dict[str, str]:
    try:
        entries = sorted(directory.iterdir())
    except FileNotFoundError as error:
        message = f"{directory}: the distribution directory is missing; run `uv build` first."
        raise ValueError(message) from error
    except OSError as error:
        message = f"{directory}: could not be listed; check the path and its permissions."
        raise ValueError(message) from error
    # `uv build` writes its own `.gitignore` into the output directory.
    entries = [entry for entry in entries if not entry.name.startswith(".")]
    wheels = [entry for entry in entries if entry.name.endswith(".whl")]
    sdists = [entry for entry in entries if entry.name.endswith(".tar.gz")]
    unexpected = sorted(set(entries) - set(wheels) - set(sdists))
    if unexpected:
        names = ", ".join(entry.name for entry in unexpected)
        message = f"{directory} holds files that are not distributions ({names}); build into an empty directory."
        raise ValueError(message)
    if len(wheels) != 1:
        message = f"{directory}: expected exactly one wheel, found {len(wheels)}; build into an empty directory."
        raise ValueError(message)
    if len(sdists) != 1:
        message = (
            f"{directory}: expected exactly one source distribution, found {len(sdists)}; "
            "build into an empty directory."
        )
        raise ValueError(message)
    return {wheels[0].name: _wheel_version(wheels[0]), sdists[0].name: _sdist_version(sdists[0])}
