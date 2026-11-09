"""`Win32Ops` against fake pywebview windows and a fake user32. The fake UI thread runs what is marshalled to
it one item at a time, as WinForms' message loop does, so a marshal made while that thread is busy waiting
for the marshal's own answer deadlocks in the test exactly as it does in the app."""

import ctypes
import queue
import sys
import threading
import types
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import pytest

from commander import buywindows
from commander.buypane import PaneArea
from commander.buywindows import Win32Ops
from commander.cmsession import BrowserError, LoadTimeout

if TYPE_CHECKING:
    from webview import Window

AREA = PaneArea(24.0, 120.0, 800.0, 420.0, 1.0)
MAIN_HWND = 1000
SCAN_HWND = 2000
GWL_EXSTYLE = -20
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_WINDOW_HANDLE = 1400
WAIT_S = 5.0


class UiThread:
    """The one thread every window's calls must run on."""

    def __init__(self) -> None:
        self._work: queue.Queue[tuple[Callable[[], None], threading.Event, list[BaseException]] | None] = (
            queue.Queue()
        )
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def on_thread(self) -> bool:
        return threading.get_ident() == self._thread.ident

    def invoke(self, work: Callable[[], None]) -> None:
        """Run `work` on the UI thread and wait for it; inline when already there."""
        if self.on_thread():
            work()
            return
        done = threading.Event()
        failure: list[BaseException] = []
        self._work.put((work, done, failure))
        assert done.wait(WAIT_S), "the fake UI thread never ran the call"
        if failure:
            raise failure[0]

    def stop(self) -> None:
        self._work.put(None)

    def _loop(self) -> None:
        while (item := self._work.get()) is not None:
            work, done, failure = item
            try:
                work()
            except BaseException as error:  # noqa: BLE001 - handed to the thread that asked
                failure.append(error)
            done.set()


class FakeHandle:
    def __init__(self, value: int) -> None:
        self._value = value

    def ToInt64(self) -> int:  # noqa: N802 - .NET's name
        return self._value


class FakeWebview:
    def __init__(self) -> None:
        self.focused: list[bool] = []

    def Focus(self) -> None:  # noqa: N802 - .NET's name
        self.focused.append(True)


class FakeNative:
    def __init__(self, ui: UiThread, hwnd: int) -> None:
        self._ui = ui
        self.Handle = FakeHandle(hwnd)
        self.Owner: object = None
        self.webview = FakeWebview()

    @property
    def InvokeRequired(self) -> bool:  # noqa: N802 - .NET's name
        return not self._ui.on_thread()

    def Invoke(self, action: Callable[[], None]) -> None:  # noqa: N802
        self._ui.invoke(action)


class FakeWinformsWindow:
    def __init__(self, ui: UiThread, hwnd: int) -> None:
        self.native = FakeNative(ui, hwnd)
        self.shown = 0

    def show(self) -> None:
        self.shown += 1


class FakeUser32:
    """Records the style and position writes; a failure returns 0 and sets the thread's last error."""

    def __init__(self) -> None:
        self.style = 0x40100
        self.origin = (100, 50)
        self.get_style_error = 0
        self.set_style_error = 0
        self.position_error = 0
        self.origin_ok = True
        self.style_writes: list[tuple[int, int, int]] = []
        self.positions: list[tuple[int, int, int, int, int, int]] = []

    def GetWindowLongPtrW(self, hwnd: int, index: int) -> int:  # noqa: N802, ARG002 - the Win32 name
        if self.get_style_error:
            ctypes.set_last_error(self.get_style_error)
            return 0
        return self.style

    def SetWindowLongPtrW(self, hwnd: int, index: int, value: int) -> int:  # noqa: N802
        if self.set_style_error:
            ctypes.set_last_error(self.set_style_error)
            return 0
        self.style_writes.append((hwnd, index, value))
        return self.style

    def ClientToScreen(self, hwnd: int, point: object) -> int:  # noqa: N802, ARG002
        if not self.origin_ok:
            return 0
        target: Any = cast("Any", point)._obj  # noqa: SLF001 - what ctypes.byref wraps
        target.x, target.y = self.origin
        return 1

    def SetWindowPos(  # noqa: N802, PLR0917
        self, hwnd: int, after: object, x: int, y: int, width: int, height: int, flags: int
    ) -> int:
        del after, flags
        if self.position_error:
            ctypes.set_last_error(self.position_error)
            return 0
        self.positions.append((hwnd, x, y, width, height, 0))
        return 1


@dataclass
class Rig:
    ops: Win32Ops
    ui: UiThread
    user32: FakeUser32
    main: FakeWinformsWindow
    scan: FakeWinformsWindow


def passthrough(work: Callable[[], None]) -> Callable[[], None]:
    """System.Action(work): the fake native takes the callable itself."""
    return work


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    ui = UiThread()
    user32 = FakeUser32()
    monkeypatch.setattr(buywindows, "_load_user32", lambda: user32)
    monkeypatch.setattr(buywindows, "CALL_TIMEOUT_S", 0.3)
    dotnet = types.ModuleType("System")
    vars(dotnet)["Action"] = passthrough
    monkeypatch.setitem(sys.modules, "System", dotnet)
    yield Rig(Win32Ops(), ui, user32, FakeWinformsWindow(ui, MAIN_HWND), FakeWinformsWindow(ui, SCAN_HWND))
    ui.stop()


def as_window(fake: FakeWinformsWindow) -> "Window":
    return cast("Window", fake)


class TestUiThreadCalls:
    def test_preparing_from_a_worker_thread_owns_the_window_and_clears_its_taskbar_style(
        self, rig: Rig
    ) -> None:
        rig.ops.prepare(as_window(rig.scan), as_window(rig.main))

        assert rig.scan.native.Owner is rig.main.native
        # 0x40100 without WS_EX_APPWINDOW (0x40000).
        assert rig.user32.style_writes == [(SCAN_HWND, GWL_EXSTYLE, 0x100)]

    def test_showing_from_the_ui_thread_focuses_the_page_instead_of_waiting_on_itself(self, rig: Rig) -> None:
        rig.ui.invoke(lambda: rig.ops.show(as_window(rig.scan)))

        assert (rig.scan.shown, rig.scan.native.webview.focused) == (1, [True])

    def test_placing_from_the_ui_thread_reads_the_handles_instead_of_waiting_on_itself(
        self, rig: Rig
    ) -> None:
        rig.ui.invoke(lambda: rig.ops.place(as_window(rig.scan), as_window(rig.main), AREA))

        # The client origin (100, 50) plus the area's 24 and 120.
        assert rig.user32.positions == [(SCAN_HWND, 124, 170, 800, 420, 0)]

    def test_showing_from_a_worker_thread_focuses_the_page_on_the_ui_thread(self, rig: Rig) -> None:
        rig.ops.show(as_window(rig.scan))

        assert rig.scan.native.webview.focused == [True]

    def test_a_ui_thread_that_does_not_answer_fails_the_call_instead_of_hanging(self, rig: Rig) -> None:
        release = threading.Event()
        started = threading.Event()

        def busy() -> None:
            started.set()
            release.wait(WAIT_S)

        threading.Thread(target=lambda: rig.ui.invoke(busy), daemon=True).start()
        assert started.wait(WAIT_S)
        try:
            with pytest.raises(LoadTimeout, match="UI thread did not answer"):
                rig.ops.show(as_window(rig.scan))
        finally:
            release.set()


class TestWin32Failures:
    def test_a_style_that_cannot_be_read_fails_the_preparing_with_the_windows_error(self, rig: Rig) -> None:
        rig.user32.get_style_error = ERROR_INVALID_WINDOW_HANDLE

        with pytest.raises(BrowserError, match=r"1400"):
            rig.ops.prepare(as_window(rig.scan), as_window(rig.main))

        assert rig.user32.style_writes == []

    def test_a_style_that_cannot_be_written_fails_the_preparing_with_the_windows_error(
        self, rig: Rig
    ) -> None:
        rig.user32.set_style_error = ERROR_ACCESS_DENIED

        with pytest.raises(BrowserError, match=r"\b5\b"):
            rig.ops.prepare(as_window(rig.scan), as_window(rig.main))

    def test_a_style_of_zero_is_no_failure_even_when_an_older_error_is_still_set(self, rig: Rig) -> None:
        rig.user32.style = 0

        def leave_an_error() -> None:
            ctypes.set_last_error(ERROR_ACCESS_DENIED)

        rig.ui.invoke(leave_an_error)

        rig.ops.prepare(as_window(rig.scan), as_window(rig.main))

        assert rig.user32.style_writes == [(SCAN_HWND, GWL_EXSTYLE, 0)]

    def test_a_window_that_cannot_be_placed_fails_with_the_windows_error(self, rig: Rig) -> None:
        rig.user32.position_error = ERROR_ACCESS_DENIED

        with pytest.raises(BrowserError, match=r"could not be placed.*\b5\b"):
            rig.ops.place(as_window(rig.scan), as_window(rig.main), AREA)

    def test_an_app_window_whose_position_cannot_be_read_fails_and_places_nothing(self, rig: Rig) -> None:
        rig.user32.origin_ok = False

        with pytest.raises(BrowserError, match="position could not be read"):
            rig.ops.place(as_window(rig.scan), as_window(rig.main), AREA)

        assert rig.user32.positions == []
