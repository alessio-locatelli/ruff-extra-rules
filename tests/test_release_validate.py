from __future__ import annotations

import io
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import pre_commit_hooks
from release import changelog, distribution, validate

if TYPE_CHECKING:
    from _pytest.capture import CaptureFixture
    from _pytest.monkeypatch import MonkeyPatch

_REPO_ROOT = Path(__file__).resolve().parent.parent

_CHANGELOG = "# Changelog\n\n## [Unreleased]\n\n## [0.1.0] - 2026-08-16\n\n- A user-visible change.\n"


def _metadata(version: str, name: str) -> bytes:
    return f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n\nA description.\n".encode()


def _decoy() -> bytes:
    return _metadata("9.9.9", "vendored")


def _directory_entry(name: str) -> tarfile.TarInfo:
    entry = tarfile.TarInfo(name)
    entry.type = tarfile.DIRTYPE
    return entry


def _build_wheel(directory: Path, version: str, *, name: str = "ruff-extra-rules") -> None:
    with zipfile.ZipFile(directory / f"ruff_extra_rules-{version}-py3-none-any.whl", "w") as archive:
        archive.writestr(f"ruff_extra_rules-{version}.dist-info/METADATA", _metadata(version, name))
        archive.writestr(f"ruff_extra_rules-{version}.data/purelib/vendored.dist-info/METADATA", _decoy())


def _build_sdist(directory: Path, version: str, *, name: str = "ruff-extra-rules") -> None:
    root = directory.parent / f"ruff_extra_rules-{version}"
    nested = root / "src" / "ruff_extra_rules.egg-info"
    nested.mkdir(parents=True, exist_ok=True)
    (root / "PKG-INFO").write_bytes(_metadata(version, name))
    (nested / "PKG-INFO").write_bytes(_decoy())
    with tarfile.open(directory / f"ruff_extra_rules-{version}.tar.gz", "w:gz") as archive:
        archive.add(root, arcname=root.name)


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    directory = tmp_path / "dist"
    directory.mkdir()
    _build_wheel(directory, "0.1.0")
    _build_sdist(directory, "0.1.0")
    return directory


def _empty_dist(tmp_path: Path) -> Path:
    directory = tmp_path / "empty"
    directory.mkdir()
    return directory


def _run_main(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    dist: Path,
    *arguments: str,
    package_version: str = "0.1.0",
    text: str = _CHANGELOG,
) -> int:
    changelog_path = tmp_path / "CHANGELOG.md"
    changelog_path.write_text(text)
    monkeypatch.setattr(pre_commit_hooks, "__version__", package_version)
    monkeypatch.setattr(
        sys,
        "argv",
        ["validate", "v0.1.0", "--dist", dist.as_posix(), "--changelog", changelog_path.as_posix(), *arguments],
    )
    return validate.main()


def test_this_repositorys_changelog_matches_its_package_version() -> None:
    parsed = changelog.parse((_REPO_ROOT / "CHANGELOG.md").read_text())

    assert parsed.latest.version == pre_commit_hooks.__version__


def test_main_accepts_a_consistent_release(monkeypatch: MonkeyPatch, tmp_path: Path, dist: Path) -> None:
    (dist / ".gitignore").write_text("*\n")

    assert _run_main(monkeypatch, tmp_path, dist) == 0


def test_main_writes_the_release_notes(monkeypatch: MonkeyPatch, tmp_path: Path, dist: Path) -> None:
    notes_path = tmp_path / "notes.md"

    assert _run_main(monkeypatch, tmp_path, dist, "--notes-out", notes_path.as_posix()) == 0
    assert notes_path.read_text() == "- A user-visible change.\n"


@pytest.mark.parametrize(
    ("package_version", "text", "expected"),
    [
        ("0.2.0", _CHANGELOG, "pre_commit_hooks.__version__ is 0.2.0"),
        (
            "0.1.0",
            "## [Unreleased]\n\n## [0.2.0] - 2026-08-16\n\n- Newer.\n\n## [0.1.0] - 2026-08-01\n\n- Older.\n",
            "newest changelog section is 0.2.0",
        ),
        (
            "0.1.0",
            "## [Unreleased]\n\n- Still pending.\n\n## [0.1.0] - 2026-08-16\n\n- A change.\n",
            "Unreleased",
        ),
        ("0.1.0", "## [Unreleased]\n\n## [0.1.0] - 2026-08-16\n", "section is empty"),
    ],
)
def test_main_rejects_a_version_mismatch(
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    dist: Path,
    *,
    package_version: str,
    text: str,
    expected: str,
) -> None:
    assert _run_main(monkeypatch, tmp_path, dist, package_version=package_version, text=text) == 1
    assert expected in capsys.readouterr().err


def test_main_rejects_a_distribution_built_from_another_version(
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    dist: Path,
) -> None:
    (dist / "ruff_extra_rules-0.1.0-py3-none-any.whl").unlink()
    _build_wheel(dist, "0.0.9")

    assert _run_main(monkeypatch, tmp_path, dist) == 1
    assert "reports version 0.0.9" in capsys.readouterr().err


@pytest.mark.parametrize("tag", ["0.1.0", "v0.1", "release-1"])
def test_main_rejects_a_tag_that_is_not_a_release(monkeypatch: MonkeyPatch, dist: Path, tag: str) -> None:
    monkeypatch.setattr(sys, "argv", ["validate", tag, "--dist", dist.as_posix()])

    with pytest.raises(SystemExit, match="2"):
        validate.main()


def test_main_reports_a_missing_changelog(
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    dist: Path,
) -> None:
    missing = tmp_path / "missing.md"
    monkeypatch.setattr(
        sys, "argv", ["validate", "v0.1.0", "--dist", dist.as_posix(), "--changelog", missing.as_posix()]
    )

    with pytest.raises(SystemExit, match="2"):
        validate.main()

    assert missing.as_posix() in capsys.readouterr().err


def test_main_reports_a_malformed_changelog(
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    dist: Path,
) -> None:
    with pytest.raises(SystemExit, match="2"):
        _run_main(monkeypatch, tmp_path, dist, text="# Changelog\n")

    assert "Unreleased" in capsys.readouterr().err


def test_main_reports_an_incomplete_dist_directory(
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit, match="2"):
        _run_main(monkeypatch, tmp_path, _empty_dist(tmp_path))

    assert "exactly one wheel" in capsys.readouterr().err


def test_main_reports_notes_that_cannot_be_written(
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    dist: Path,
) -> None:
    with pytest.raises(SystemExit, match="2"):
        _run_main(monkeypatch, tmp_path, dist, "--notes-out", tmp_path.as_posix())

    assert tmp_path.as_posix() in capsys.readouterr().err


@pytest.mark.parametrize("misnamed", ["wheel", "sdist"])
def test_versions_rejects_a_distribution_of_another_project(tmp_path: Path, misnamed: str) -> None:
    directory = _empty_dist(tmp_path)
    _build_wheel(directory, "0.1.0", name="something-else" if misnamed == "wheel" else "ruff-extra-rules")
    _build_sdist(directory, "0.1.0", name="something-else" if misnamed == "sdist" else "ruff-extra-rules")

    with pytest.raises(ValueError, match="something-else"):
        distribution.versions(directory)


@pytest.mark.parametrize(
    ("filenames", "expected"),
    [
        (["ruff_extra_rules-0.1.0-py3-none-any.whl"], "exactly one source distribution"),
        (["ruff_extra_rules-0.1.0.tar.gz"], "exactly one wheel"),
        (
            ["a-0.1.0-py3-none-any.whl", "b-0.1.0-py3-none-any.whl", "ruff_extra_rules-0.1.0.tar.gz"],
            "exactly one wheel",
        ),
        (["ruff_extra_rules-0.1.0-py3-none-any.whl", "ruff_extra_rules-0.1.0.tar.gz", "left.txt"], "left.txt"),
        (["ruff_extra_rules-0.1.0-py3-none-any.whl", "ruff_extra_rules-0.1.0.tar.gz", ".stray"], ".stray"),
    ],
)
def test_versions_rejects_an_unexpected_dist_directory(tmp_path: Path, filenames: list[str], expected: str) -> None:
    directory = _empty_dist(tmp_path)
    for filename in filenames:
        (directory / filename).write_text("placeholder")

    with pytest.raises(ValueError, match=expected):
        distribution.versions(directory)


def test_versions_reports_a_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing"):
        distribution.versions(tmp_path / "absent")


def test_versions_reports_an_unreadable_directory(monkeypatch: MonkeyPatch, dist: Path) -> None:
    monkeypatch.setattr(Path, "iterdir", lambda _: (_ for _ in ()).throw(PermissionError("denied")))

    with pytest.raises(ValueError, match="could not be listed"):
        distribution.versions(dist)


@pytest.mark.parametrize("filename", ["ruff_extra_rules-0.1.0-py3-none-any.whl", "ruff_extra_rules-0.1.0.tar.gz"])
def test_versions_reports_an_unreadable_archive(dist: Path, filename: str) -> None:
    (dist / filename).write_text("not an archive")

    with pytest.raises(ValueError, match="could not be read"):
        distribution.versions(dist)


def test_versions_reports_a_wheel_without_metadata(dist: Path) -> None:
    with zipfile.ZipFile(dist / "ruff_extra_rules-0.1.0-py3-none-any.whl", "w") as archive:
        archive.writestr("ruff_extra_rules-0.1.0.dist-info/RECORD", "")

    with pytest.raises(ValueError, match="METADATA"):
        distribution.versions(dist)


@pytest.mark.parametrize(
    "entry",
    [
        tarfile.TarInfo("ruff_extra_rules-0.1.0/README.md"),
        _directory_entry("ruff_extra_rules-0.1.0/PKG-INFO"),
    ],
)
def test_versions_reports_an_sdist_without_readable_metadata(dist: Path, entry: tarfile.TarInfo) -> None:
    with tarfile.open(dist / "ruff_extra_rules-0.1.0.tar.gz", "w:gz") as archive:
        archive.addfile(entry, io.BytesIO(b""))

    with pytest.raises(ValueError, match="PKG-INFO"):
        distribution.versions(dist)


def test_versions_reports_metadata_without_a_version(dist: Path) -> None:
    with zipfile.ZipFile(dist / "ruff_extra_rules-0.1.0-py3-none-any.whl", "w") as archive:
        archive.writestr("ruff_extra_rules-0.1.0.dist-info/METADATA", "Name: ruff-extra-rules\n")

    with pytest.raises(ValueError, match="Version"):
        distribution.versions(dist)
