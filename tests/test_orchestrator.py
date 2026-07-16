"""Tests for CheckOrchestrator (ast_checks/__init__.py)."""

from __future__ import annotations

from pathlib import Path

from pre_commit_hooks.ast_checks import CheckOrchestrator, load_checks
from pre_commit_hooks.ast_checks.forbid_vars import ForbidVarsCheck


def test_process_files_handles_utf8_bom(tmp_path: Path) -> None:
    """A UTF-8 BOM must not make the orchestrator silently skip the file.

    filepath.read_text(encoding="utf-8") decodes a leading BOM as a literal
    U+FEFF character, which ast.parse rejects as a syntax error — reading
    with utf-8-sig strips it transparently instead (and is identical to
    utf-8 for files without one).
    """
    filepath = tmp_path / "with_bom.py"
    filepath.write_bytes(b"\xef\xbb\xbfdata = 1\n")

    orchestrator = CheckOrchestrator(checks=[ForbidVarsCheck()])
    violations = orchestrator.process_files([str(filepath)])

    assert len(violations[str(filepath)]) == 1
    assert violations[str(filepath)][0].error_code == "TRI001"


def test_apply_fixes_handles_utf8_bom(tmp_path: Path) -> None:
    """The re-read before each check's fix() call must also strip a BOM.

    The fixed file keeps its original BOM on write (detected encoding is
    "utf-8-sig", the same encoding used to write back) — reading it back
    with "utf-8-sig" strips it again, same as the original read.
    """
    filepath = tmp_path / "with_bom.py"
    filepath.write_bytes(b"\xef\xbb\xbfdata = requests.get(url)\n")

    orchestrator = CheckOrchestrator(checks=[ForbidVarsCheck()], fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])
    fix_data = violations[str(filepath)][0].fix_data

    assert fix_data is not None
    assert fix_data["fixed"] is True
    assert filepath.read_bytes().startswith(b"\xef\xbb\xbf")
    assert filepath.read_text(encoding="utf-8-sig") == "response = requests.get(url)\n"


def test_apply_fixes_recomputes_stale_positions(tmp_path: Path) -> None:
    """A later check's fix() must not use line numbers from before an earlier
    check's fix already rewrote the file in the same --fix run.

    excessive-blank-lines runs (and fixes) before redundant-assignment in
    ALL_CHECKS order. Collapsing the 3 blank lines after the module docstring
    down to 2 removes one line, shifting `x = "foo"`/`print(x)` up by one —
    so if redundant-assignment's fix() were handed the violation positions
    collected before that collapse, it would edit the wrong (now-shifted)
    lines and silently fail to inline `x`.
    """
    filepath = tmp_path / "stale_positions.py"
    filepath.write_text(
        '"""Module docstring."""\n'
        "\n\n\n"
        "def func_scope():\n"
        '    x = "foo"\n'
        "    print(x)\n"
    )

    checks = load_checks(enabled={"excessive-blank-lines", "redundant-assignment"})
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    redundant_assignment_fixed = any(
        v.check_id == "redundant-assignment" and v.fix_data and v.fix_data.get("fixed")
        for v in violations[str(filepath)]
    )
    assert redundant_assignment_fixed

    result = filepath.read_text(encoding="utf-8")
    assert 'x = "foo"' not in result
    assert "print(" in result
    assert '"foo"' in result


def test_fix_honors_pep263_encoding_declaration(tmp_path: Path) -> None:
    """A file with a non-UTF-8 PEP 263 encoding cookie must be read, fixed,
    and written back in its declared encoding, not assumed to be UTF-8.
    """
    source = "# -*- coding: latin-1 -*-\nresult = func(\n    x\n)  # caf\xe9\n"
    filepath = tmp_path / "latin1.py"
    filepath.write_bytes(source.encode("latin-1"))

    checks = load_checks(enabled={"misplaced-comment"})
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    assert violations[str(filepath)][0].fix_data == {"fixed": True}
    result = filepath.read_bytes().decode("latin-1")
    assert "x  # caf\xe9" in result
    assert ")\n" in result


def test_fix_preserves_crlf_line_endings(tmp_path: Path) -> None:
    """Lines untouched by a fix must keep their original CRLF endings."""
    filepath = tmp_path / "crlf.py"
    filepath.write_bytes(
        b"result = func(\r\n    x\r\n)  # comment\r\n\r\nother = 1\r\n"
    )

    checks = load_checks(enabled={"misplaced-comment"})
    orchestrator = CheckOrchestrator(checks=checks, fix_mode=True)
    violations = orchestrator.process_files([str(filepath)])

    assert violations[str(filepath)][0].fix_data == {"fixed": True}
    result = filepath.read_bytes()
    # Untouched lines keep their original CRLF endings.
    assert b"\r\nother = 1\r\n" in result
    assert b"x  # comment" in result
