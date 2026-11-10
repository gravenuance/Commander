"""Live test of the Cardmarket scan (ADR 0067): reads the offers of the cards named on the command line.

Opens a small visible status window and a hidden Cardmarket window on the same WebView2 profile, restores the
session the app saved in Credential Manager, runs the two-pass scan one page at a time and prints progress and
a summary. Every window is closed with `destroy()` at the end, also after an error: killing the process leaves
WebView2 holding the profile and the next start shows a white page.

    uv run python -X utf8 tools/cardmarket_scan.py --profile <folder> "Sol Ring" "Arcane Signet"
    uv run python -X utf8 tools/cardmarket_scan.py --profile <folder> --wants-list 1234567 "Sol Ring"
    uv run python -X utf8 tools/cardmarket_scan.py --profile <folder> --save-pages <dir> --visit <url> ...

Sign in through the app's Buy screen first: this tool has no sign-in handover, so a signed-out session
fails with `SignedOut`. `--profile` must be the app's Cardmarket storage folder
(`%LOCALAPPDATA%/Commander/webview`), and the app must be closed, as it holds that folder.

`--visit` loads more Cardmarket pages after the scan (or instead of it, when no card is named), at the scan's
pace. `--save-pages` writes every page the tool loads to the folder as numbered HTML, for making test
fixtures; trim them and replace seller and user names, user ids and `__cmtkn` tokens before committing.
"""

from __future__ import annotations

import argparse
import logging
import random
import re
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from commander.cmpages import Condition
from commander.cmscan import (
    DEFAULT_BASE_SELLERS,
    ENGLISH,
    MAX_GAP_S,
    MIN_GAP_S,
    ScanFilters,
    ScanProgress,
    ScanRequest,
    ScanResult,
    scan,
)
from commander.cmsession import BrowserError, WebviewBrowser, on_cardmarket
from commander.credentials import CardmarketSessionStore, CredentialError
from commander.sellersolve import WantedCard

if TYPE_CHECKING:
    from webview import Window

    from commander.credentials import SessionStore

DESCRIPTION = "Scan Cardmarket for the offers of the named cards, as the app's Buy screen will."
STATUS_PAGE = (
    "<body style='font-family:sans-serif;padding:1em'><h3>Cardmarket scan</h3>"
    "<p>Reading pages one at a time, 4 to 6 seconds apart. Close this window to cancel.</p></body>"
)
_NOT_SLUG = re.compile(r"[^A-Za-z0-9]+")


class RecordingBrowser(WebviewBrowser):
    """A `WebviewBrowser` that also writes each page it loads to `folder` as numbered HTML, when given one."""

    def __init__(self, window: Window, store: SessionStore, folder: Path | None) -> None:
        super().__init__(window, store)
        self._folder = folder
        self._saved = 0

    def load(self, url: str, *, cancellable: bool = True) -> str:
        html = super().load(url, cancellable=cancellable)
        if self._folder is not None:
            self._saved += 1
            slug = _NOT_SLUG.sub("-", urlsplit(url).path).strip("-")[:80] or "page"
            target = self._folder / f"{self._saved:02d}-{slug}.html"
            target.write_text(html, encoding="utf-8")
            print(f"saved {target}", flush=True)
        return html


def print_progress(progress: ScanProgress) -> None:
    print(f"[{progress.done}/{progress.planned}] {progress.stage.value}: {progress.current}", flush=True)


def print_summary(result: ScanResult) -> None:
    print(f"\nscan {result.status.value}", end="")
    print(f": {result.stop_reason}" if result.stop_reason else "")
    print(f"pages read: {result.pages_read}; buyable offers: {len(result.offers)}")
    print(f"destination: {result.destination or 'unknown'}")
    print(f"base sellers: {', '.join(result.base_sellers) or 'none'}")
    for label, names in (
        ("not found", result.not_found),
        ("no buyable offer", result.no_buyable),
        ("more offers than read", result.cards_with_more),
        ("more stock pages than read", result.sellers_with_more_pages),
    ):
        if names:
            print(f"{label}: {', '.join(names)}")
    cheapest: dict[str, int] = {}
    for offer in result.offers:
        cheapest[offer.card] = min(offer.price_cents, cheapest.get(offer.card, offer.price_cents))
    for card, cents in sorted(cheapest.items()):
        print(f"  {card}: from {cents // 100},{cents % 100:02d} EUR")


def visit_pages(browser: RecordingBrowser, urls: list[str], cancel: threading.Event) -> None:
    """Load each address in turn, a gap after the last request of the browser (the scan's, if it ran)."""
    uniform = random.uniform  # the pacing's jitter, not a secret
    for url in urls:
        browser.pacer.wait(lambda: uniform(MIN_GAP_S, MAX_GAP_S), cancel.wait)
        if cancel.is_set():
            return
        print(f"visiting {url}", flush=True)
        try:
            browser.load(url)
        finally:
            browser.pacer.mark()


def run_scan(status: Window, args: argparse.Namespace, cancel: threading.Event) -> None:
    """Runs on the thread pywebview starts: build the scan window, scan, close every window."""
    import webview  # noqa: PLC0415 - loads .NET, so only after the arguments are read

    scan_window: Window | None = None
    try:
        # pywebview types its url parameter with an untyped callable.
        scan_window = webview.create_window(  # pyright: ignore[reportUnknownMemberType]
            "Cardmarket scan", html=STATUS_PAGE, hidden=True
        )
        if scan_window is None:
            print("pywebview created no scan window", file=sys.stderr)
            return
        browser = RecordingBrowser(scan_window, CardmarketSessionStore(), args.save_pages)
        if args.cards:
            request = ScanRequest(
                wanted=[WantedCard(name, 1) for name in args.cards],
                filters=ScanFilters(languages=tuple(args.language), min_condition=args.condition),
                wants_list_id=args.wants_list,
                base_sellers=args.base_sellers,
            )
            print_summary(scan(browser, request, cancel=cancel, on_progress=print_progress))
        visit_pages(browser, args.visit, cancel)
    except (BrowserError, CredentialError) as error:
        print(f"scan failed: {error}", file=sys.stderr)
    finally:
        for window in (scan_window, status):
            if window is not None:
                try:
                    window.destroy()
                except Exception as error:  # noqa: BLE001 - the other windows must still close
                    print(f"could not close a window: {error!r}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("cards", nargs="*", metavar="CARD", help="card names, as the wants list names them")
    parser.add_argument(
        "--profile",
        type=Path,
        required=True,
        help="the app's Cardmarket storage folder, %%LOCALAPPDATA%%/Commander/webview",
    )
    parser.add_argument("--wants-list", type=int, help="the wants list id; without it there is no pass 2")
    parser.add_argument(
        "--language",
        type=int,
        nargs="+",
        default=[ENGLISH],
        help="Cardmarket language ids (1 English, 7 Japanese)",
    )
    parser.add_argument(
        "--condition", choices=[c.value for c in Condition], help="the worst condition to accept"
    )
    parser.add_argument("--base-sellers", type=int, default=DEFAULT_BASE_SELLERS, help="sellers for pass 2")
    parser.add_argument("--visit", nargs="+", default=[], metavar="URL", help="more Cardmarket pages to load")
    parser.add_argument("--save-pages", type=Path, metavar="DIR", help="write every page loaded to DIR")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every page address")
    args = parser.parse_args()
    if not args.cards and not args.visit:
        parser.error("name at least one card, or give --visit")
    if bad := [url for url in args.visit if not on_cardmarket(url)]:
        parser.error(f"not a Cardmarket page: {bad[0]}")
    args.condition = Condition(args.condition) if args.condition else None
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr)

    import webview  # noqa: PLC0415 - loads .NET, so only after the arguments are read

    cancel = threading.Event()
    args.profile.mkdir(parents=True, exist_ok=True)
    if args.save_pages is not None:
        args.save_pages.mkdir(parents=True, exist_ok=True)
    # pywebview types its url parameter with an untyped callable.
    status = webview.create_window(  # pyright: ignore[reportUnknownMemberType]
        "Cardmarket scan", html=STATUS_PAGE, width=480, height=200
    )
    if status is None:
        print("pywebview created no window", file=sys.stderr)
        return 1
    # Closing the status window cancels the scan; the scan thread then closes the rest.
    status.events.closed += cancel.set
    webview.start(run_scan, (status, args, cancel), private_mode=False, storage_path=str(args.profile))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
