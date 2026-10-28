"""Importing the command line loads neither the HTTP library nor the window: much of its start-up."""

import subprocess
import sys

CHECK = """
import sys
import commander.cli
loaded = sorted(name for name in ("httpx", "commander.app", "commander.desk") if name in sys.modules)
print(",".join(loaded))
"""


def test_importing_the_cli_loads_neither_httpx_nor_the_desktop_app() -> None:
    result = subprocess.run(  # noqa: S603 — the test's own interpreter and a fixed script
        [sys.executable, "-X", "utf8", "-c", CHECK], capture_output=True, text=True, check=True, timeout=60
    )
    assert result.stdout.strip() == ""
