"""Live test of the wants-list sync (ADR 0067): fills the app's Cardmarket wants list with the named cards.

Opens a small visible status window and a hidden Cardmarket window on the same WebView2 profile, restores the
session saved by `tools/cardmarket_login.py` (or the app) from Credential Manager, then finds or creates the
list named `Commander`, empties it, imports the cards, sets the language and minimum condition and reads the
list back. It changes that one list and no other. Every window is closed with `destroy()` at the end, also
after an error: killing the process leaves WebView2 holding the profile and the next start shows a white page.

    uv run python -X utf8 tools/cardmarket_wants.py --profile <folder> "Sol Ring" "Arcane Signet=2"
    uv run python -X utf8 tools/cardmarket_wants.py --profile <folder> --file wants.txt --condition NM

A card is `NAME` or `NAME=QUANTITY`; `--file` takes lines as `cmdr toget --cardmarket` prints them
(`<quantity> <name>`). The profile folder should be the one the sign-in used.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from commander.cmpages import Condition, PageError
from commander.cmscan import ENGLISH
from commander.cmsession import BrowserError, WebviewBrowser
from commander.cmwants import APP_LIST_NAME, WantsCancelled, WantsProgress, WantsSync, sync_wants
from commander.credentials import CardmarketSessionStore, CredentialError
from commander.sellersolve import WantedCard

if TYPE_CHECKING:
    from webview import Window

DESCRIPTION = "Fill the app's Cardmarket wants list with the named cards, as the app's Buy screen will."
STATUS_PAGE = (
    "<body style='font-family:sans-serif;padding:1em'><h3>Cardmarket wants list</h3>"
    "<p>Working one request at a time, 4 to 6 seconds apart. Close this window to cancel.</p></body>"
)
_EXPORT_LINE = re.compile(r"(?P<quantity>\d+)\s+(?P<name>\S.*)")


def parse_card(text: str) -> WantedCard:
    """`NAME` or `NAME=QUANTITY`."""
    name, separator, quantity = text.rpartition("=")
    if separator and quantity.strip().isdigit():
        return WantedCard(name.strip(), int(quantity))
    return WantedCard(text.strip(), 1)


def read_export(path: Path) -> list[WantedCard]:
    """The lines of `cmdr toget --cardmarket` output: `<quantity> <name>`."""
    wanted: list[WantedCard] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        match = _EXPORT_LINE.fullmatch(line.strip())
        if match is None:
            raise ValueError(f"{path}:{number}: not a '<quantity> <name>' line: {line!r}")
        wanted.append(WantedCard(match["name"], int(match["quantity"])))
    return wanted


def print_progress(progress: WantsProgress) -> None:
    print(f"{progress.step.value}: {progress.detail}", flush=True)


def print_summary(result: WantsSync) -> None:
    print(f"\nlist {result.list_id} holds {len(result.imported)} wants")
    if result.unmatched:
        print(f"not matched by Cardmarket: {', '.join(result.unmatched)}")
    for mismatch in result.quantity_mismatch:
        print(f"quantity differs: {mismatch.card} wanted {mismatch.wanted}, list has {mismatch.got}")
    for want in result.imported:
        languages = "/".join(want.languages) or "any language"
        print(f"  {want.quantity} {want.card} ({languages}, {want.min_condition.value} or better)")


def run_sync(status: Window, args: argparse.Namespace, cancel: threading.Event) -> None:
    """Runs on the thread pywebview starts: build the Cardmarket window, sync, close every window."""
    import webview  # noqa: PLC0415 - loads .NET, so only after the arguments are read

    sync_window: Window | None = None
    try:
        # pywebview types its url parameter with an untyped callable.
        sync_window = webview.create_window("Cardmarket wants", html=STATUS_PAGE, hidden=True)  # pyright: ignore[reportUnknownMemberType]
        if sync_window is None:
            print("pywebview created no window", file=sys.stderr)
            return
        browser = WebviewBrowser(sync_window, CardmarketSessionStore())
        result = sync_wants(
            browser,
            args.wanted,
            language=args.language,
            min_condition=args.condition,
            cancel=cancel,
            on_progress=print_progress,
        )
        print_summary(result)
    except WantsCancelled:
        print("cancelled; the list was left as far as it got", file=sys.stderr)
    except (BrowserError, PageError, CredentialError) as error:
        print(f"sync failed: {error}", file=sys.stderr)
    finally:
        for window in (sync_window, status):
            if window is not None:
                try:
                    window.destroy()
                except Exception as error:  # noqa: BLE001 - the other windows must still close
                    print(f"could not close a window: {error!r}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("cards", nargs="*", metavar="CARD", help="NAME or NAME=QUANTITY")
    parser.add_argument("--file", type=Path, help="lines of '<quantity> <name>' (toget --cardmarket output)")
    parser.add_argument("--profile", type=Path, required=True, help="the WebView2 profile folder to share")
    parser.add_argument("--language", type=int, default=ENGLISH, help="Cardmarket language id (1 English)")
    parser.add_argument("--any-language", action="store_true", help="leave the language as imported")
    parser.add_argument(
        "--condition", choices=[c.value for c in Condition], help="the worst condition to accept"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log every page address")
    args = parser.parse_args()
    wanted = [parse_card(text) for text in args.cards]
    if args.file is not None:
        wanted.extend(read_export(args.file))
    if not wanted:
        parser.error("name at least one card, or give --file")
    args.wanted = wanted
    args.language = None if args.any_language else args.language
    args.condition = Condition(args.condition) if args.condition else None
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr)

    import webview  # noqa: PLC0415 - loads .NET, so only after the arguments are read

    cancel = threading.Event()
    args.profile.mkdir(parents=True, exist_ok=True)
    copies = sum(w.quantity for w in wanted)
    print(f"list: {APP_LIST_NAME}; {copies} cards in {len(wanted)} lines", flush=True)
    # pywebview types its url parameter with an untyped callable.
    status = webview.create_window("Cardmarket wants", html=STATUS_PAGE, width=480, height=200)  # pyright: ignore[reportUnknownMemberType]
    if status is None:
        print("pywebview created no window", file=sys.stderr)
        return 1
    # Closing the status window cancels the sync; the sync thread then closes the rest.
    status.events.closed += cancel.set
    webview.start(run_sync, (status, args, cancel), private_mode=False, storage_path=str(args.profile))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
