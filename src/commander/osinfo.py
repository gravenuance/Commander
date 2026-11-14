"""Which system the program runs on, in one place. `platform` is a `sys.platform` value; leave it out for the
running system, and give one in tests."""

import sys


def current(platform: str | None = None) -> str:
    return sys.platform if platform is None else platform


def is_windows(platform: str | None = None) -> bool:
    return current(platform) == "win32"


def is_linux(platform: str | None = None) -> bool:
    return current(platform).startswith("linux")
