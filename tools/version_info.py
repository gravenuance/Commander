"""Write the Windows version resource PyInstaller stamps on Commander.exe (`--version-file`), so Explorer's
Details tab shows the app's name, version and copyright.

Run from the repo root: `uv run python tools/version_info.py <out.txt>`. The version comes from pyproject.toml
and the copyright line from LICENSE; it needs only the standard library.
"""

import re
import sys
import tomllib
from pathlib import Path

NAME = "Commander"
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def version_info(version: str, copyright_line: str) -> str:
    """The resource as PyInstaller's version-file syntax: a VSVersionInfo expression."""
    match = _VERSION.fullmatch(version)
    if match is None:
        raise ValueError(f"{version} is not a version like 1.2.3")
    numbers = (*(int(part) for part in match.groups()), 0)
    strings = {
        "CompanyName": "",
        "FileDescription": NAME,
        "FileVersion": version,
        "InternalName": NAME,
        "LegalCopyright": copyright_line,
        "OriginalFilename": f"{NAME}.exe",
        "ProductName": NAME,
        "ProductVersion": version,
    }
    structs = ",\n          ".join(f"StringStruct({key!r}, {value!r})" for key, value in strings.items())
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numbers},
    prodvers={numbers},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          {structs}
        ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: version_info.py <out.txt>", file=sys.stderr)
        return 2
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    license_lines = Path("LICENSE").read_text(encoding="utf-8").splitlines()
    copyright_line = next((line.strip() for line in license_lines if line.startswith("Copyright")), "")
    try:
        text = version_info(str(project["version"]), copyright_line)
    except ValueError as error:
        print(f"version_info: {error}", file=sys.stderr)
        return 1
    out = Path(argv[0])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
