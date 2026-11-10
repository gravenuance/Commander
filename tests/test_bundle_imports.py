"""PyInstaller finds what to bundle by reading import statements, so a module loaded only through
`importlib.import_module` is missing from the exe: the solver once failed there with
"No module named 'numpy'"."""

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "src" / "commander"

# Names the exe gets by other means: .NET's System comes from pythonnet at run time, and keyring's own
# imports already bundle win32ctypes.
ALLOWED = {"System", "win32ctypes.pywin32.pywintypes"}


def _dynamic_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "import_module" and node.args:
            match node.args[0]:
                case ast.Constant(value=str() as name):
                    names.append(name)
                case _:
                    names.append("?")
    return names


def test_every_module_the_app_loads_by_name_is_one_the_exe_gets_otherwise() -> None:
    found = {
        f"{path.relative_to(SOURCE)}: {name}"
        for path in sorted(SOURCE.rglob("*.py"))
        for name in _dynamic_imports(path)
        if name not in ALLOWED
    }

    assert found == set()
