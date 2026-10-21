"""tools/version_info.py: the Windows version resource the exe build stamps on Commander.exe."""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "version_info.py"
LICENSE = "MIT License\n\nCopyright (c) 2026 markos\n\nPermission is hereby granted"


def run(tmp_path: Path, version: str) -> subprocess.CompletedProcess[str]:
    """The script run in a folder holding only a pyproject.toml with this version and a LICENSE."""
    (tmp_path / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n', encoding="utf-8")
    (tmp_path / "LICENSE").write_text(LICENSE, encoding="utf-8")
    return subprocess.run(  # noqa: S603 - this interpreter and a fixed script, no shell
        [sys.executable, "-I", str(SCRIPT), "build/version.txt"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_version_resource_names_the_app_and_carries_the_projects_version(tmp_path: Path) -> None:
    assert run(tmp_path, "1.2.3").returncode == 0
    text = (tmp_path / "build" / "version.txt").read_text(encoding="utf-8")

    assert "filevers=(1, 2, 3, 0)" in text
    assert "prodvers=(1, 2, 3, 0)" in text
    for key, value in [
        ("FileDescription", "Commander"),
        ("ProductName", "Commander"),
        ("FileVersion", "1.2.3"),
        ("ProductVersion", "1.2.3"),
        ("OriginalFilename", "Commander.exe"),
        ("LegalCopyright", "Copyright (c) 2026 markos"),
    ]:
        assert f"StringStruct('{key}', '{value}')" in text


def test_a_version_that_is_not_three_numbers_is_refused(tmp_path: Path) -> None:
    result = run(tmp_path, "1.0.0rc1")

    assert result.returncode == 1
    assert "1.0.0rc1 is not a version like 1.2.3" in result.stderr
    assert not (tmp_path / "build" / "version.txt").exists()
