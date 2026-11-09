"""The area of the Buy screen the Cardmarket window is shown over while the owner signs in (ADR 0067).

The page reports the area of its empty pane element in CSS pixels; the window code turns that into the
screen's physical pixels. Both steps are pure, so they are tested without a window.
"""

import math
from dataclasses import dataclass
from typing import Protocol

from commander.buysettings import BuyError


@dataclass(slots=True, frozen=True)
class PaneArea:
    """An element's rectangle in the app window's client area, in CSS pixels, and the pixels per CSS pixel."""

    left: float
    top: float
    width: float
    height: float
    scale: float


class PaneAreaSink(Protocol):
    def set_area(self, area: PaneArea | None) -> None:
        """The page's latest area, or None when the page shows no pane."""
        ...


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise BuyError("The sign-in area was not valid.")
    return float(value)


def parse_pane_area(
    left: object, top: object, width: object, height: object, scale: object
) -> PaneArea | None:
    """The area the page reported; None when it reports no pane (an empty width or height, or no scale)."""
    numbers = [_number(value) for value in (left, top, width, height, scale)]
    area = PaneArea(*numbers)
    if area.width <= 0 or area.height <= 0 or area.scale <= 0:
        return None
    return area


def screen_rect(origin: tuple[int, int], area: PaneArea) -> tuple[int, int, int, int]:
    """The area as x, y, width and height in physical screen pixels, given the screen position of the app
    window's client origin."""
    return (
        round(origin[0] + area.left * area.scale),
        round(origin[1] + area.top * area.scale),
        round(area.width * area.scale),
        round(area.height * area.scale),
    )
