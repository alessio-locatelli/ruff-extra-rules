from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

UNRELEASED = "Unreleased"

_HEADING = re.compile(r"^## \[(?P<label>[^\]]*)\](?:\s+-\s+(?P<date>.*?))?\s*$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class Release:
    body: str
    date: str
    version: str


@dataclass(frozen=True)
class Changelog:
    releases: tuple[Release, ...]
    unreleased: str

    @property
    def latest(self) -> Release:
        return self.releases[0]

    def notes(self, version: str) -> str:
        for release in self.releases:
            if release.version == version:
                return release.body
        message = f"the changelog has no `## [{version}]` section; add one before releasing {version}."
        raise ValueError(message)


@dataclass(frozen=True)
class _Section:
    body: str
    date: str | None
    label: str


def _split_sections(text: str) -> list[_Section]:
    headings: list[re.Match[str]] = []
    bodies: list[list[str]] = []
    for line in text.splitlines():
        heading = _HEADING.match(line)
        if heading is not None:
            headings.append(heading)
            bodies.append([])
        elif bodies:
            bodies[-1].append(line)
    return [
        _Section(body="\n".join(body).strip(), date=heading["date"], label=heading["label"])
        for heading, body in zip(headings, bodies, strict=True)
    ]


def _needs_a_date(label: str) -> ValueError:
    return ValueError(f"`## [{label}]` needs a `- YYYY-MM-DD` date after the version.")


def _release(section: _Section) -> Release:
    if not _VERSION.match(section.label):
        message = f"`## [{section.label}]` must be a MAJOR.MINOR.PATCH version, not `{section.label}`."
        raise ValueError(message)
    if section.date is None or not _DATE.match(section.date):
        raise _needs_a_date(section.label)
    try:
        date.fromisoformat(section.date)
    except ValueError as error:
        raise _needs_a_date(section.label) from error
    return Release(body=section.body, date=section.date, version=section.label)


def _ordered(releases: list[Release]) -> tuple[Release, ...]:
    keys = [tuple(int(part) for part in release.version.split(".")) for release in releases]
    for index, key in enumerate(keys[1:], start=1):
        if key == keys[index - 1]:
            message = f"the changelog lists {releases[index].version} more than once; keep one section per version."
            raise ValueError(message)
        if key > keys[index - 1]:
            message = (
                f"{releases[index].version} is listed below {releases[index - 1].version}; "
                "order the sections newest first."
            )
            raise ValueError(message)
    return tuple(releases)


def parse(text: str) -> Changelog:
    sections = _split_sections(text)
    if not sections or sections[0].label != UNRELEASED:
        message = f"the changelog needs an `## [{UNRELEASED}]` section, and it must come first."
        raise ValueError(message)
    if any(section.label == UNRELEASED for section in sections[1:]):
        message = f"the changelog has more than one `## [{UNRELEASED}]` section; keep exactly one, at the top."
        raise ValueError(message)
    releases = [_release(section) for section in sections[1:]]
    if not releases:
        message = "the changelog has no releases; add a `## [MAJOR.MINOR.PATCH] - YYYY-MM-DD` section."
        raise ValueError(message)
    return Changelog(releases=_ordered(releases), unreleased=sections[0].body)
