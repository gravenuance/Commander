"""The meta report as JSON."""

from collections.abc import Iterator
from dataclasses import replace

from commander.metajson import report_json
from commander.playstyle import PlayStyle, Style
from test_cli_meta import report_of


class ReverseOrderStyles(frozenset[Style]):
    """A set that iterates against the enum order, as a hash-seeded set may in some process."""

    def __iter__(self) -> Iterator[Style]:
        return iter([style for style in reversed(list(Style)) if style in self])


def test_the_play_style_chips_come_in_the_enum_order_whatever_the_sets_iteration_order() -> None:
    chosen = ReverseOrderStyles({Style.COMBO, Style.TURBO, Style.STAX})
    report = replace(report_of(()), style=PlayStyle(chosen))
    assert report_json(report)["playStyle"] == ["turbo", "stax", "combo"]
