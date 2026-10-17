"""The app icon: a multi-size card back whose corners and surroundings stay transparent."""

import struct
import sys
import types
import zlib
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from commander import app
from commander.desk import Desk

ICON = resources.files("commander") / "web" / "commander.ico"


def entries() -> dict[int, bytes]:
    """The icon's PNG images by side length."""
    data = ICON.read_bytes()
    _, kind, count = struct.unpack_from("<HHH", data)
    assert kind == 1
    found: dict[int, bytes] = {}
    for index in range(count):
        side, _, _, _, _, bits, length, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * index)
        assert bits == 32
        found[side or 256] = data[offset : offset + length]
    return found


def alphas(image: bytes) -> list[list[int]]:
    """Each pixel's alpha, from an unfiltered 8-bit RGBA PNG like the ones the icon holds."""
    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    width, height, depth, colour = struct.unpack_from(">IIBB", image, 16)
    assert (depth, colour) == (8, 6)
    at, idat = 8, b""
    while at < len(image):
        (length,) = struct.unpack_from(">I", image, at)
        if image[at + 4 : at + 8] == b"IDAT":
            idat += image[at + 8 : at + 8 + length]
        at += 12 + length
    raw = zlib.decompress(idat)
    stride = 1 + 4 * width
    rows = [raw[row * stride : (row + 1) * stride] for row in range(height)]
    assert all(row[0] == 0 for row in rows)
    return [list(row[4::4]) for row in rows]


def test_the_icon_holds_every_size_windows_asks_for() -> None:
    assert sorted(entries()) == [16, 32, 48, 64, 128, 256]


def test_the_card_is_opaque_and_its_corners_and_surroundings_are_clear() -> None:
    large = alphas(entries()[32])
    assert large[16][16] == 255
    assert large[16][0] == 0  # beside the card
    assert large[1][5] == 0  # the card's top-left corner
    assert 0 < large[1][6] < 255  # the step beside it
    small = alphas(entries()[16])
    assert 0 < small[0][2] < 255  # the small card's corner is half-clear


def test_the_window_shows_the_icon(monkeypatch: pytest.MonkeyPatch) -> None:
    started: dict[str, Any] = {}
    fake = types.ModuleType("webview")
    fake.create_window = lambda *_args, **_kwargs: None  # type: ignore[attr-defined]
    fake.start = lambda **kwargs: started.update(kwargs)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "webview", fake)
    app.run_app(Desk.__new__(Desk))
    assert Path(started["icon"]).name == "commander.ico"
    assert Path(started["icon"]).is_file()
