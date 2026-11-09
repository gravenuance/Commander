"""Live test of the Cardmarket scan (ADR 0067): reads the offers of the cards named on the command line.

Opens a small visible status window and a hidden Cardmarket window on the same WebView2 profile, restores the
session saved by `tools/cardmarket_login.py` (or the app) from Credential Manager, runs the two-pass scan one
page at a time and prints progress and a summary. Every window is closed with `destroy()` at the end, also
after an error: killing the process leaves WebView2 holding the profile and the next start shows a white page.

    uv run python -X utf8 tools/cardmarket_scan.py --profile <folder> "Sol Ring" "Arcane Signet"
    uv run python -X utf8 tools/cardmarket_scan.py --profile <folder> --wants-list 1234567 "Sol Ring"

The profile folder should be the one the sign-in used. Without a saved session the scan stops at the first
page that asks to sign in; sign in first with `tools/cardmarket_login.py`.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from commander.cmpages import Condition
from commander.cmscan import (
    DEFAULT_BASE_SELLERS,
    ENGLISH,
    ScanFilters,
    ScanProgress,
    ScanRequest,
    ScanResult,
    scan,
)
from commander.cmsession import BrowserError, WebviewBrowser
from commander.credentials import CardmarketSessionStore, CredentialError
from commander.sellersolve import WantedCard

if TYPE_CHECKING:
    from webview import Window

DESCRIPTION = "Scan Cardmarket for the offers of the named cards, as the app's Buy screen will."
STATUS_PAGE = (
    "<body style='font-family:sans-serif;padding:1em'><h3>Cardmarket scan</h3>"
    "<p>Reading pages one at a time, 4 to 6 seconds apart. Close this window to cancel.</p></body>"
)


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


def run_scan(status: Window, args: argparse.Namespace, cancel: threading.Event) -> None:
    """Runs on the thread pywebview starts: build the scan window, scan, close every window."""
    import webview  # noqa: PLC0415 - loads .NET, so only after the arguments are read

    scan_window: Window | None = None
    try:
        # pywebview types its url parameter with an untyped callable.
        scan_window = webview.create_window("Cardmarket scan", html=STATUS_PAGE, hidden=True)  # pyright: ignore[reportUnknownMemberType]
        if scan_window is None:
            print("pywebview created no scan window", file=sys.stderr)
            return
        browser = WebviewBrowser(scan_window, CardmarketSessionStore())
        request = ScanRequest(
            wanted=[WantedCard(name, 1) for name in args.cards],
            filters=ScanFilters(languages=tuple(args.language), min_condition=args.condition),
            wants_list_id=args.wants_list,
            base_sellers=args.base_sellers,
        )
        print_summary(scan(browser, request, cancel=cancel, on_progress=print_progress))
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
    parser.add_argument("cards", nargs="+", metavar="CARD", help="card names, as the wants list names them")
    parser.add_argument("--profile", type=Path, required=True, help="the WebView2 profile folder to share")
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
    parser.add_argument("-v", "--verbose", action="store_true", help="log every page address")
    args = parser.parse_args()
    args.condition = Condition(args.condition) if args.condition else None
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr)

    import webview  # noqa: PLC0415 - loads .NET, so only after the arguments are read

    cancel = threading.Event()
    args.profile.mkdir(parents=True, exist_ok=True)
    # pywebview types its url parameter with an untyped callable.
    status = webview.create_window("Cardmarket scan", html=STATUS_PAGE, width=480, height=200)  # pyright: ignore[reportUnknownMemberType]
    if status is None:
        print("pywebview created no window", file=sys.stderr)
        return 1
    # Closing the status window cancels the scan; the scan thread then closes the rest.
    status.events.closed += cancel.set
    webview.start(run_scan, (status, args, cancel), private_mode=False, storage_path=str(args.profile))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
