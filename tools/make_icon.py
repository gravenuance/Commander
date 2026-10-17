"""Draw the app icon: a low-pixel card back, written as src/commander/web/commander.ico.

Run from the repo root: `uv run python tools/make_icon.py [preview.png]`. It needs only the standard library.
The 16 and 32 pixel icons are each drawn at their own size; the larger sizes are whole-number blow-ups of
them, so every pixel stays crisp. Corners fade out through partial alpha, and outside the card is clear.
"""

import math
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

type Rgb = tuple[int, int, int]
type Rgba = tuple[int, int, int, int]
type Image = list[list[Rgba]]

CLEAR: Rgba = (0, 0, 0, 0)
OUTLINE: Rgb = (30, 18, 9)
FRAME_LIGHT: Rgb = (154, 106, 58)
FRAME: Rgb = (110, 69, 36)
INNER_LINE: Rgb = (42, 26, 13)
FIELD: Rgb = (35, 58, 115)
SWIRL: Rgb = (62, 98, 168)
RIM: Rgb = (200, 144, 47)
OVAL: Rgb = (138, 79, 31)
# White, blue, black, red and green, clockwise from the top.
PIPS: tuple[Rgb, ...] = ((244, 239, 201), (47, 127, 208), (28, 22, 20), (216, 64, 46), (47, 154, 74))
OUT = Path("src/commander/web/commander.ico")
LARGE = 32
# Squared distance from the oval's centre, as a share of its radius, inside which the oval is filled.
OVAL_FILL = 0.62
# The swirl's light bands: where its wave rises above this.
SWIRL_ABOVE = 0.55


@dataclass(frozen=True)
class Card:
    """Where the card sits in an icon of one size, and how the small and large drawings differ."""

    size: int

    @property
    def big(self) -> bool:
        return self.size == LARGE

    @property
    def left(self) -> int:
        return 5 if self.big else 2

    @property
    def top(self) -> int:
        return 1 if self.big else 0

    @property
    def right(self) -> int:
        return self.size - 1 - self.left

    @property
    def bottom(self) -> int:
        return self.size - 1 - self.top

    @property
    def centre(self) -> tuple[float, float]:
        return (self.left + self.right) / 2, (self.top + self.bottom) / 2

    @property
    def oval(self) -> tuple[float, float]:
        width, height = (0.36, 0.33) if self.big else (0.32, 0.28)
        return (self.right - self.left) * width, (self.bottom - self.top) * height


def draw(size: int) -> Image:
    """The card back at 16 or 32 pixels, in its own pixel grid."""
    card = Card(size)
    image = [[CLEAR] * size for _ in range(size)]
    for y in range(card.top, card.bottom + 1):
        for x in range(card.left, card.right + 1):
            image[y][x] = (*body(card, x, y), 255)
    round_corners(card, image)
    place_pips(card, image)
    return image


def body(card: Card, x: int, y: int) -> Rgb:
    """The outline, the frame and (on the large card) an inner line from the edge in, then the field."""
    frame = 3 if card.big else 2
    edge = min(x - card.left, card.right - x, y - card.top, card.bottom - y)
    if edge == 0:
        return OUTLINE
    if edge < frame:
        return FRAME_LIGHT if edge == 1 and card.big else FRAME
    if edge == frame and card.big:
        return INNER_LINE
    return field(card, x, y)


def field(card: Card, x: int, y: int) -> Rgb:
    """The blue swirl around the central oval, its gold rim and the oval itself."""
    (cx, cy), (rx, ry) = card.centre, card.oval
    reach = ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2
    if reach <= OVAL_FILL:
        return OVAL
    if reach <= 1:
        return RIM
    return SWIRL if math.sin((x + y) * 0.7 + 2.2 * math.sin(y * 0.4)) > SWIRL_ABOVE else FIELD


def round_corners(card: Card, image: Image) -> None:
    """The large card's corners go clear, half-clear either side; the small card's go half-clear."""
    for x in (card.left, card.right):
        for y in (card.top, card.bottom):
            round_corner(card, image, x, y)


def round_corner(card: Card, image: Image, x: int, y: int) -> None:
    if not card.big:
        image[y][x] = (*OUTLINE, 110)
        return
    image[y][x] = CLEAR
    image[y][x + (1 if x == card.left else -1)] = (*OUTLINE, 150)
    image[y + (1 if y == card.top else -1)][x] = (*OUTLINE, 150)


def place_pips(card: Card, image: Image) -> None:
    """Five colour pips in a pentagon in the oval: 2 by 2 on the large card, one pixel on the small."""
    (cx, cy), (rx, _) = card.centre, card.oval
    side = 2 if card.big else 1
    for index, colour in enumerate(PIPS):
        angle = -math.pi / 2 + index * 2 * math.pi / 5
        px = round(cx + rx * 0.55 * math.cos(angle)) - side // 2
        py = round(cy + rx * 0.55 * math.sin(angle)) - side // 2
        for dy in range(side):
            for dx in range(side):
                image[py + dy][px + dx] = (*colour, 255)


def blow_up(image: Image, factor: int) -> Image:
    return [[pixel for pixel in row for _ in range(factor)] for row in image for _ in range(factor)]


def png(image: Image) -> bytes:
    """The image as a 32-bit RGBA PNG."""
    width, height = len(image[0]), len(image)
    raw = b"".join(b"\x00" + bytes(channel for pixel in row for channel in pixel) for row in image)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    chunks = chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    return b"\x89PNG\r\n\x1a\n" + chunks


def ico(images: list[Image]) -> bytes:
    """An icon file holding each image as a PNG entry, as Windows Vista and later read them."""
    blobs = [png(image) for image in images]
    offset = 6 + 16 * len(blobs)
    entries = b""
    for image, blob in zip(images, blobs, strict=True):
        side = len(image) % 256  # 0 stands for 256
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    return struct.pack("<HHH", 0, 1, len(blobs)) + entries + b"".join(blobs)


def main() -> None:
    small, large = draw(16), draw(LARGE)
    sizes = [small, large, blow_up(small, 3), blow_up(large, 2), blow_up(large, 4), blow_up(large, 8)]
    OUT.write_bytes(ico(sizes))
    print(f"wrote {OUT} ({', '.join(str(len(image)) for image in sizes)} px)")
    if len(sys.argv) > 1:
        # Side by side: the 16 px icon blown up 8 times and the 32 px one blown up 4 times.
        pair = zip(blow_up(small, 8), blow_up(large, 4), strict=True)
        Path(sys.argv[1]).write_bytes(png([left + right for left, right in pair]))


if __name__ == "__main__":
    main()
