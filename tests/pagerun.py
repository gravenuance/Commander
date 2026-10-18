"""Runs the app page's real scripts under Node, against a fake DOM built from the real index.html.

Node is a test-time tool only: tests that use `page` skip, with the reason shown, when it is not on PATH.
`pagerun.js` documents what a program can use.
"""

import json
import shutil
import subprocess
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

HARNESS = Path(__file__).with_name("pagerun.js")
WEB = Path(str(resources.files("commander") / "web"))
NODE = shutil.which("node")

needs_node = pytest.mark.skipif(NODE is None, reason="node is not on PATH; the page behaviour tests need it")


# The JSON a program returned, of whatever shape that test's program builds.
type Answer = Any


class PageError(AssertionError):
    """The program threw inside the page; the message is the JavaScript stack."""


def page(program: str) -> Answer:
    """Run the body of an async JavaScript function against the loaded page and return its JSON result."""
    assert NODE is not None
    done = subprocess.run(  # noqa: S603 - node and a fixed script, no shell
        [NODE, str(HARNESS)],
        input=json.dumps({"web": str(WEB), "program": program}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    try:
        answer = json.loads(done.stdout)
    except json.JSONDecodeError:
        raise PageError(f"node exited {done.returncode}: {done.stderr or done.stdout}") from None
    if "error" in answer:
        raise PageError(answer["error"])
    return answer["result"]
