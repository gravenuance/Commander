"""Prototype: can Commander keep a Cardmarket session that the user signed in to by hand, 2FA included?

Opens Cardmarket in a WebView2 window with its own persistent profile. Sign in there yourself (password,
2FA, any Cloudflare check), then close the window. Cardmarket's session lives in the PHPSESSID cookie, which
WebView2 drops on close, so the prototype keeps it alive two ways:

- While signed in, each page load saves the session cookie to Windows Credential Manager; the next run puts
  it back before Cardmarket loads, so a session the server still honours carries on.
- WebView2 may offer to save the password (as Edge does, encrypted in the profile), so signing in again after
  the server ends the session is one click; the trusted-device cookie already skips 2FA for 30 days.

After every page load the script prints whether the page looks signed in and which cookies exist, with
their expiry; cookie values are never printed.

    uv run python -X utf8 tools/cardmarket_login.py           # sign in, close the window
    uv run python -X utf8 tools/cardmarket_login.py           # run again: still signed in?
    uv run python -X utf8 tools/cardmarket_login.py --forget  # delete the profile and the saved session
"""

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

from commander.config import cache_dir

if TYPE_CHECKING:
    from webview import Window

DESCRIPTION = "Open Cardmarket in a persistent WebView2 profile and report whether the session survives."

START_URL = "https://www.cardmarket.com/en/Magic"
HOST = "www.cardmarket.com"
PROFILE_FOLDER = "cardmarket-webview"
SESSION_COOKIE = "PHPSESSID"
KEYRING_SERVICE = "Commander (cmdr) Cardmarket"
KEYRING_USER = "session-cookie"
# Windows Credential Manager holds at most 1280 characters per secret; a PHP session id is far shorter.
MAX_SECRET_LENGTH = 1280
BLANK_PAGE = "<p style='font-family:sans-serif'>Starting…</p>"

# Signs of a signed-in page, read from the DOM because the session cookies are HttpOnly anyway. Cardmarket's
# markup is undocumented, so the prototype reports each sign on its own rather than trusting one.
SIGNED_IN_PROBE = """
JSON.stringify({
  url: location.href,
  loginForm: !!document.querySelector('input[name="username"], input[type="password"]'),
  logoutLink: !!document.querySelector('a[href*="Logout" i], form[action*="Logout" i]'),
  accountLink: !!document.querySelector('a[href*="/Account" i]'),
  cloudflare: document.title.includes('Just a moment')
})
"""


@dataclass(slots=True, frozen=True)
class SavedCookie:
    name: str
    value: str
    domain: str
    path: str

    def to_json(self) -> str:
        return json.dumps({"name": self.name, "value": self.value, "domain": self.domain, "path": self.path})

    @staticmethod
    def from_json(text: str) -> "SavedCookie | None":
        """The saved cookie, or None when the secret isn't one this script wrote."""
        try:
            data: object = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        fields = cast("dict[str, object]", data)
        values = [fields.get(key) for key in ("name", "value", "domain", "path")]
        if not all(isinstance(value, str) and value for value in values):
            return None
        name, value, domain, path = cast("list[str]", values)
        if name != SESSION_COOKIE or domain.lstrip(".") not in (HOST, HOST.removeprefix("www.")):
            return None
        return SavedCookie(name, value, domain, path)


def describe_cookies(cookies: list[SimpleCookie]) -> list[str]:
    """One line per cookie: domain, name and expiry, never the value."""
    lines: list[str] = []
    for jar in cookies:
        for name, morsel in jar.items():
            expires = morsel["expires"] or "session (gone when the window closes)"
            lines.append(f"  {morsel['domain'] or '?'}  {name}  expires {expires}")
    return sorted(lines)


def session_cookie(cookies: list[SimpleCookie]) -> SavedCookie | None:
    for jar in cookies:
        morsel = jar.get(SESSION_COOKIE)
        if morsel is not None and morsel.value:
            return SavedCookie(SESSION_COOKIE, morsel.value, morsel["domain"] or HOST, morsel["path"] or "/")
    return None


def on_cardmarket(url: str | None) -> bool:
    """True for a page on Cardmarket itself, matched on the parsed hostname."""
    host = (urlsplit(url or "").hostname or "").rstrip(".")
    return urlsplit(url or "").scheme == "https" and host == HOST


class SessionStore:
    """The saved session cookie, in Windows Credential Manager next to the app's other secrets."""

    def __init__(self) -> None:
        self._last: str | None = None

    def load(self) -> SavedCookie | None:
        import keyring  # noqa: PLC0415

        text = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        return SavedCookie.from_json(text) if text else None

    def save(self, cookie: SavedCookie) -> None:
        """Store the cookie unless it is the one already stored this run."""
        text = cookie.to_json()
        if text == self._last or len(text) > MAX_SECRET_LENGTH:
            return
        import keyring  # noqa: PLC0415

        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, text)
        self._last = text
        print("saved the session cookie to Credential Manager", flush=True)

    def forget(self) -> None:
        import keyring  # noqa: PLC0415
        import keyring.errors  # noqa: PLC0415

        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except keyring.errors.PasswordDeleteError:
            return


def report(window: "Window", store: SessionStore) -> None:
    """Print the sign-in probe and cookies for the page that just loaded; save the session if signed in."""
    if not on_cardmarket(window.get_current_url()):
        return
    try:
        probe = cast("dict[str, object]", json.loads(window.evaluate_js(SIGNED_IN_PROBE)))
        print(f"\npage: {json.dumps(probe)}")
        # pywebview leaves get_cookies untyped; on WebView2 it returns one SimpleCookie per cookie.
        cookies = cast("list[SimpleCookie]", window.get_cookies())  # pyright: ignore[reportUnknownMemberType]
        print(f"cookies for this page ({len(cookies)}):")
        # Flushed, so each page load's report shows up at once when the output goes to a file.
        print("\n".join(describe_cookies(cookies)) or "  none", flush=True)
        cookie = session_cookie(cookies)
        if probe.get("logoutLink") is True and cookie is not None:
            store.save(cookie)
    except Exception as error:  # noqa: BLE001 - a probe failure must not close the window the user signs in on
        print(f"probe failed: {error!r}", file=sys.stderr, flush=True)


def prepare(window: "Window", store: SessionStore) -> None:
    """Once WebView2 is up: turn on password autofill, restore the saved session, then open Cardmarket."""
    window.events.loaded.wait()
    saved = store.load()
    # pythonnet's .NET objects are untyped; pywebview has loaded the CLR by now.
    from System import Action  # noqa: PLC0415  # pyright: ignore[reportMissingImports, reportUnknownVariableType]

    native: Any = cast("Any", window.native)

    def on_ui_thread() -> None:
        core: Any = native.webview.CoreWebView2
        core.Settings.IsPasswordAutosaveEnabled = True
        core.Settings.IsGeneralAutofillEnabled = True
        if saved is not None:
            cookie: Any = core.CookieManager.CreateCookie(saved.name, saved.value, saved.domain, saved.path)
            cookie.IsSecure = True
            cookie.IsHttpOnly = True
            core.CookieManager.AddOrUpdateCookie(cookie)

    # WebView2 only takes calls on the window's UI thread.
    native.Invoke(Action(on_ui_thread))
    print("restored the saved session cookie" if saved else "no saved session cookie", flush=True)
    window.load_url(START_URL)


def main() -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--forget", action="store_true", help="delete the profile and the saved session")
    args = parser.parse_args()
    profile = cache_dir(os.environ) / PROFILE_FOLDER
    store = SessionStore()
    if args.forget:
        shutil.rmtree(profile, ignore_errors=True)
        store.forget()
        print(f"deleted {profile} and the saved session cookie")
        return 0

    import webview  # noqa: PLC0415 - loads .NET, so only after the arguments are read

    print(f"profile: {profile}")
    # pywebview types its url parameter with an untyped callable.
    window = webview.create_window("Cardmarket sign-in (prototype)", html=BLANK_PAGE, width=1200, height=900)  # pyright: ignore[reportUnknownMemberType]
    if window is None:
        print("pywebview created no window", file=sys.stderr)
        return 1
    window.events.loaded += lambda: report(window, store)
    webview.start(prepare, (window, store), private_mode=False, storage_path=str(profile))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
