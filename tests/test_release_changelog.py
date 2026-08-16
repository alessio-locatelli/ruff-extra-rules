from __future__ import annotations

import pytest

from release import changelog

_MINIMAL = """# Changelog

## [Unreleased]

## [0.2.0] - 2026-08-16

### Changed

- Something users can notice.

## [0.1.0] - 2026-08-01

- The first one.
"""


def test_parse_reads_every_release_newest_first() -> None:
    parsed = changelog.parse(_MINIMAL)

    assert [release.version for release in parsed.releases] == ["0.2.0", "0.1.0"]
    assert parsed.releases[0].date == "2026-08-16"
    assert parsed.releases[0].body == "### Changed\n\n- Something users can notice."


def test_parse_keeps_the_unreleased_section_separate() -> None:
    parsed = changelog.parse("# Changelog\n\n## [Unreleased]\n\n- Pending.\n\n## [0.1.0] - 2026-08-01\n\n- Shipped.\n")

    assert parsed.unreleased == "- Pending."
    assert [release.version for release in parsed.releases] == ["0.1.0"]


def test_notes_returns_one_releases_body() -> None:
    assert changelog.parse(_MINIMAL).notes("0.1.0") == "- The first one."


def test_notes_rejects_an_unknown_version() -> None:
    with pytest.raises(ValueError, match=r"9\.9\.9"):
        changelog.parse(_MINIMAL).notes("9.9.9")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("# Changelog\n", "needs an `## \\[Unreleased\\]` section"),
        ("# Changelog\n\n## [0.1.0] - 2026-08-01\n", "needs an `## \\[Unreleased\\]` section"),
        ("## [0.1.0] - 2026-08-01\n\n## [Unreleased]\n", "must come first"),
        ("## [Unreleased]\n\n## [Unreleased]\n", "more than one"),
        ("## [Unreleased]\n\n## [0.1.0]\n", "needs a `- YYYY-MM-DD` date"),
        ("## [Unreleased]\n\n## [0.1.0] - 01-08-2026\n", "needs a `- YYYY-MM-DD` date"),
        ("## [Unreleased]\n\n## [0.1.0] - 2026-02-30\n", "needs a `- YYYY-MM-DD` date"),
        ("## [Unreleased]\n\n## [v0.1.0] - 2026-08-01\n", "not `v0.1.0`"),
        ("## [Unreleased]\n\n## [0.1] - 2026-08-01\n", "not `0.1`"),
        (
            "## [Unreleased]\n\n## [0.1.0] - 2026-08-01\n\n## [0.1.0] - 2026-07-01\n",
            "lists 0.1.0 more than once",
        ),
        (
            "## [Unreleased]\n\n## [0.1.0] - 2026-08-01\n\n## [0.2.0] - 2026-09-01\n",
            "newest first",
        ),
        ("## [Unreleased]\n", "has no releases"),
    ],
)
def test_parse_rejects_a_malformed_changelog(text: str, expected: str) -> None:
    with pytest.raises(ValueError, match=expected):
        changelog.parse(text)
