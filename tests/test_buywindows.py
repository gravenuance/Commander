"""When the pane places, shows and hides the Cardmarket window, with the window calls recorded instead."""

from collections.abc import Callable
from typing import TYPE_CHECKING, cast

import pytest

from commander.buypane import PaneArea
from commander.buywindows import CardmarketPane, PaneHandover
from commander.cmsession import BrowserError, Need

if TYPE_CHECKING:
    from webview import Window

AREA = PaneArea(24.0, 120.0, 800.0, 420.0, 1.0)
MOVED_AREA = PaneArea(24.0, 150.0, 800.0, 390.0, 1.0)


class FakeWindow:
    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return self.name


def window(name: str) -> "Window":
    return cast("Window", FakeWindow(name))


MAIN = window("main")
SCAN = window("scan")


class FakeOps:
    """Records each call as a tuple; `moved` is the callback the pane gave to watch the main window."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.moved: Callable[..., None] | None = None

    def prepare(self, window: "Window", main: "Window") -> None:
        self.calls.append(("prepare", window, main))

    def place(self, window: "Window", main: "Window", area: PaneArea) -> None:
        self.calls.append(("place", window, main, area))

    def show(self, window: "Window") -> None:
        self.calls.append(("show", window))

    def hide(self, window: "Window") -> None:
        self.calls.append(("hide", window))

    def watch_moves(self, main: "Window", on_moved: Callable[..., None]) -> None:
        self.calls.append(("watch", main))
        self.moved = on_moved

    def unwatch_moves(self, main: "Window", on_moved: Callable[..., None]) -> None:
        assert on_moved == self.moved
        self.calls.append(("unwatch", main))
        self.moved = None


class FailingOps(FakeOps):
    """Raises from the named call (after recording it) until `failing` is cleared."""

    def __init__(self, failing: str, *, also_hide: bool = False) -> None:
        super().__init__()
        self.failing: str | None = failing
        self.also_hide = also_hide

    def place(self, window: "Window", main: "Window", area: PaneArea) -> None:
        super().place(window, main, area)
        if self.failing == "place":
            raise BrowserError("place failed")

    def show(self, window: "Window") -> None:
        super().show(window)
        if self.failing == "show":
            raise BrowserError("show failed")

    def hide(self, window: "Window") -> None:
        super().hide(window)
        if self.also_hide:
            raise BrowserError("hide failed")


def pane_of(ops: FakeOps, main: "Window | None" = MAIN) -> CardmarketPane:
    return CardmarketPane(ops, lambda: main)


class TestCardmarketPane:
    def test_covering_prepares_the_window_once_and_watches_the_main_window(self) -> None:
        ops = FakeOps()
        pane = pane_of(ops)

        pane.cover(SCAN)

        assert ops.calls == [("prepare", SCAN, MAIN), ("watch", MAIN)]

    def test_the_window_is_shown_when_the_first_area_arrives_and_not_before(self) -> None:
        ops = FakeOps()
        pane = pane_of(ops)
        pane.cover(SCAN)
        ops.calls.clear()

        pane.set_area(AREA)

        assert ops.calls == [("place", SCAN, MAIN, AREA), ("show", SCAN)]

    def test_an_area_known_before_the_cover_is_placed_then_shown_at_once(self) -> None:
        ops = FakeOps()
        pane = pane_of(ops)
        pane.set_area(AREA)
        assert ops.calls == []

        pane.cover(SCAN)

        assert ops.calls == [
            ("prepare", SCAN, MAIN),
            ("watch", MAIN),
            ("place", SCAN, MAIN, AREA),
            ("show", SCAN),
        ]

    def test_a_new_area_moves_a_shown_window_without_showing_it_again(self) -> None:
        ops = FakeOps()
        pane = pane_of(ops)
        pane.cover(SCAN)
        pane.set_area(AREA)
        ops.calls.clear()

        pane.set_area(MOVED_AREA)

        assert ops.calls == [("place", SCAN, MAIN, MOVED_AREA)]

    def test_no_area_hides_the_window_and_the_next_area_shows_it_again(self) -> None:
        ops = FakeOps()
        pane = pane_of(ops)
        pane.cover(SCAN)
        pane.set_area(AREA)
        ops.calls.clear()

        pane.set_area(None)
        pane.set_area(None)
        pane.set_area(AREA)

        assert ops.calls == [("hide", SCAN), ("place", SCAN, MAIN, AREA), ("show", SCAN)]

    def test_no_area_before_the_first_one_hides_nothing(self) -> None:
        ops = FakeOps()
        pane = pane_of(ops)
        pane.cover(SCAN)
        ops.calls.clear()

        pane.set_area(None)

        assert ops.calls == []

    def test_an_area_reported_while_nothing_is_covered_touches_no_window(self) -> None:
        ops = FakeOps()

        pane_of(ops).set_area(AREA)

        assert ops.calls == []

    def test_moving_the_main_window_places_the_shown_window_again(self) -> None:
        ops = FakeOps()
        pane = pane_of(ops)
        pane.cover(SCAN)
        pane.set_area(AREA)
        ops.calls.clear()
        assert ops.moved is not None

        ops.moved(100, 200)

        assert ops.calls == [("place", SCAN, MAIN, AREA)]

    def test_uncovering_stops_watching_hides_the_window_and_ignores_later_reports(self) -> None:
        ops = FakeOps()
        pane = pane_of(ops)
        pane.cover(SCAN)
        pane.set_area(AREA)
        ops.calls.clear()

        pane.uncover(SCAN)
        pane.set_area(MOVED_AREA)

        assert ops.calls == [("unwatch", MAIN), ("hide", SCAN)]

    def test_uncovering_a_window_that_is_not_covered_does_nothing(self) -> None:
        ops = FakeOps()
        pane = pane_of(ops)
        pane.cover(SCAN)
        ops.calls.clear()

        pane.uncover(window("other"))
        pane.uncover(SCAN)
        pane.uncover(SCAN)

        assert ops.calls == [("unwatch", MAIN), ("hide", SCAN)]

    def test_covering_again_shows_it_again_without_preparing_it_twice(self) -> None:
        ops = FakeOps()
        pane = pane_of(ops)
        pane.cover(SCAN)
        pane.set_area(AREA)
        pane.uncover(SCAN)
        ops.calls.clear()

        pane.cover(SCAN)

        assert ops.calls == [("watch", MAIN), ("place", SCAN, MAIN, AREA), ("show", SCAN)]

    def test_a_new_window_is_prepared_again(self) -> None:
        ops = FakeOps()
        pane = pane_of(ops)
        pane.cover(SCAN)
        pane.uncover(SCAN)
        ops.calls.clear()
        second = window("second scan")

        pane.cover(second)

        assert ops.calls[0] == ("prepare", second, MAIN)

    def test_without_the_apps_window_covering_fails_and_shows_nothing(self) -> None:
        ops = FakeOps()

        with pytest.raises(BrowserError, match="app's window is not available"):
            pane_of(ops, main=None).cover(SCAN)

        assert ops.calls == []

    @pytest.mark.parametrize("failing", ["place", "show"])
    def test_a_cover_that_fails_is_undone_so_the_retry_watches_the_main_window_once(
        self, failing: str
    ) -> None:
        ops = FailingOps(failing)
        pane = pane_of(ops)
        pane.set_area(AREA)

        with pytest.raises(BrowserError, match=f"{failing} failed"):
            pane.cover(SCAN)

        assert ops.calls[-2:] == [("unwatch", MAIN), ("hide", SCAN)]
        assert ops.moved is None
        ops.calls.clear()
        pane.set_area(MOVED_AREA)
        assert ops.calls == []

        ops.failing = None
        pane.cover(SCAN)

        assert ops.calls == [("watch", MAIN), ("place", SCAN, MAIN, MOVED_AREA), ("show", SCAN)]
        assert ops.moved is not None

    def test_a_cover_that_fails_and_cannot_hide_the_window_still_raises_the_first_failure(self) -> None:
        ops = FailingOps("show", also_hide=True)
        pane = pane_of(ops)
        pane.set_area(AREA)

        with pytest.raises(BrowserError, match="show failed"):
            pane.cover(SCAN)

        assert ops.moved is None


class FakeScreen:
    """The screen's half of a handover."""

    def __init__(self, calls: list[tuple[object, ...]]) -> None:
        self.calls = calls
        self.asked_cancel = False

    def show(self, need: Need) -> None:
        self.calls.append(("screen show", need))

    def hide(self) -> None:
        self.calls.append(("screen hide",))

    def cancelled(self) -> bool:
        return self.asked_cancel


class TestPaneHandover:
    def test_showing_tells_the_screen_then_covers_once_however_often_the_need_changes(self) -> None:
        ops = FakeOps()
        screen = FakeScreen(ops.calls)
        handover = PaneHandover(screen, pane_of(ops), SCAN)

        handover.show(Need.CHECK)
        handover.show(Need.SIGN_IN)

        assert ops.calls == [
            ("screen show", Need.CHECK),
            ("prepare", SCAN, MAIN),
            ("watch", MAIN),
            ("screen show", Need.SIGN_IN),
        ]

    def test_hiding_uncovers_then_tells_the_screen_and_a_second_show_covers_again(self) -> None:
        ops = FakeOps()
        screen = FakeScreen(ops.calls)
        handover = PaneHandover(screen, pane_of(ops), SCAN)
        handover.show(Need.SIGN_IN)
        ops.calls.clear()

        handover.hide()
        handover.show(Need.CHECK)

        assert ops.calls == [
            ("unwatch", MAIN),
            ("hide", SCAN),
            ("screen hide",),
            ("screen show", Need.CHECK),
            ("watch", MAIN),
        ]

    def test_hiding_what_was_never_shown_only_tells_the_screen(self) -> None:
        ops = FakeOps()
        handover = PaneHandover(FakeScreen(ops.calls), pane_of(ops), SCAN)

        handover.hide()

        assert ops.calls == [("screen hide",)]

    def test_the_screen_is_still_told_when_the_window_will_not_hide(self) -> None:
        class Stuck(FakeOps):
            def hide(self, window: "Window") -> None:  # noqa: ARG002 - the protocol's parameter name
                raise BrowserError("stuck")

        ops = Stuck()
        handover = PaneHandover(FakeScreen(ops.calls), pane_of(ops), SCAN)
        handover.show(Need.SIGN_IN)

        with pytest.raises(BrowserError, match="stuck"):
            handover.hide()

        assert ops.calls[-1] == ("screen hide",)

    def test_cancelled_is_the_screens_answer(self) -> None:
        ops = FakeOps()
        screen = FakeScreen(ops.calls)
        handover = PaneHandover(screen, pane_of(ops), SCAN)

        assert handover.cancelled() is False
        screen.asked_cancel = True
        assert handover.cancelled() is True
