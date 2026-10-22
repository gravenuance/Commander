"""What the desktop app's start-up imports leave unloaded, so the window opens before they are paid for."""

import subprocess
import sys

import pytest

# What `app_main._start` imports before it builds the window.
_START_UP = (
    "import commander.app, commander.board, commander.carddb, commander.config, commander.credentials, "
    "commander.net"
)


def run_python(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - this interpreter and a fixed script, no shell
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize("module", ["httpx", "keyring"])
def test_the_start_up_imports_do_not_load_the_heavy_modules(module: str) -> None:
    result = run_python(f"{_START_UP}\nimport sys\nraise SystemExit(int({module!r} in sys.modules))")
    assert result.returncode == 0, f"{module} is loaded by the start-up imports\n{result.stderr}"


def test_the_first_client_loads_httpx() -> None:
    result = run_python(
        "import sys\nfrom commander.net import make_client\nmake_client().close()\n"
        "assert 'httpx' in sys.modules"
    )
    assert result.returncode == 0, result.stderr
